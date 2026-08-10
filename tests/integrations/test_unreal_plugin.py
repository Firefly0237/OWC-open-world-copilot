"""OWCopilotBridge content-only UE plugin: descriptor shape, offline importability, the pure
read-merge-refill merge, and the engine-driving importer exercised against a fake ``unreal``.

The plugin lives outside the Python package (it ships with the UE project), so its
``Content/Python`` is put on ``sys.path`` here and imported directly — the same way the editor
exposes it. Everything is verified with NO engine: ``import unreal`` is guarded to None, and the
write path is driven through a fake that records the JSON handed to
``fill_data_table_from_json_string`` so the destructive empty-and-fill workaround is proven to
preserve untouched rows. The exported ``unreal/`` dirs come from the real exporter (no hand-rolled
shapes), so plan/import tests reuse production output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_PY = _REPO_ROOT / "integrations" / "unreal" / "OWCopilotBridge" / "Content" / "Python"
if str(_PLUGIN_PY) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_PY))

import owcopilot_bridge  # noqa: E402
from owcopilot_bridge import importer as importer_mod  # noqa: E402
from owcopilot_bridge.merge import merge_rows_by_name  # noqa: E402

from owcopilot.content.models import (  # noqa: E402
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueRef,
    DialogueTree,
    Effect,
    Entity,
    EntityType,
    Quest,
    QuestLogic,
    QuestStage,
    Relation,
    StageLogic,
)
from owcopilot.exporters import export_unreal_datatables  # noqa: E402

_UPLUGIN = _REPO_ROOT / "integrations" / "unreal" / "OWCopilotBridge" / "OWCopilotBridge.uplugin"


def _bundle() -> ContentBundle:
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
                objective="巡视边境线。",
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
                text="站住！",
                locale="zh-CN",
            )
        },
        relations=[Relation(source="npc_mara", target="loc_town", kind="resides_in")],
    )


# --- fake engine -------------------------------------------------------------------------------


class _FakeTable:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = list(rows)

    def get_editor_property(self, name: str) -> object:
        return "RowStruct-sentinel" if name == "RowStruct" else None


class _FakeDataTableLibrary:
    def __init__(self) -> None:
        self.fill_return = True
        self.fill_calls: list[tuple[_FakeTable, str, object]] = []

    def export_data_table_to_json_string(self, table: _FakeTable) -> str:
        return json.dumps(table.rows, ensure_ascii=False)

    def fill_data_table_from_json_string(
        self, table: _FakeTable, json_string: str, import_row_struct: object = None
    ) -> bool:
        self.fill_calls.append((table, json_string, import_row_struct))
        if self.fill_return:
            table.rows = json.loads(json_string)
        return self.fill_return


class _FakeEditorAssetLibrary:
    def __init__(self) -> None:
        self.save_return = True
        self.saved: list[_FakeTable] = []

    def save_loaded_asset(self, table: _FakeTable) -> bool:
        self.saved.append(table)
        return self.save_return


class _FakeUnreal:
    def __init__(self, tables: dict[str, _FakeTable]) -> None:
        self._tables = tables
        self.DataTableFunctionLibrary = _FakeDataTableLibrary()
        self.EditorAssetLibrary = _FakeEditorAssetLibrary()

    def load_asset(self, path: str) -> _FakeTable | None:
        return self._tables.get(path)


# --- descriptor + importability ----------------------------------------------------------------


def test_uplugin_is_content_only() -> None:
    data = json.loads(_UPLUGIN.read_text(encoding="utf-8"))
    assert data["CanContainContent"] is True
    assert "Modules" not in data
    assert "Source" not in data
    # depends on the Python plugin so enabling ours pulls it
    assert any(p.get("Name") == "PythonScriptPlugin" for p in data.get("Plugins", []))


def test_public_callables_present_without_engine() -> None:
    for name in ("import_bundle", "import_rows", "plan_bundle_import", "merge_rows_by_name"):
        assert callable(getattr(owcopilot_bridge, name))
    # the package imports fine with no engine present
    assert importer_mod.unreal is None


# --- pure merge (read-merge-refill) ------------------------------------------------------------


def test_merge_preserves_untouched_and_upserts() -> None:
    a = {"Name": "a", "V": 1}
    b = {"Name": "b", "V": 2}
    c = {"Name": "c", "V": 3}
    outcome = merge_rows_by_name([a, b, c], [{"Name": "b", "V": 20}, {"Name": "d", "V": 4}])
    assert [r["Name"] for r in outcome.rows] == ["a", "b", "c", "d"]
    assert outcome.rows[0] is a  # untouched rows preserved by identity (verbatim)
    assert outcome.rows[2] is c
    assert outcome.rows[1] == {"Name": "b", "V": 20}  # matched row replaced wholesale
    assert (outcome.added, outcome.updated, outcome.unchanged) == (1, 1, 2)


def test_merge_identical_reupsert_not_counted_as_update() -> None:
    outcome = merge_rows_by_name([{"Name": "a", "V": 1}], [{"Name": "a", "V": 1}])
    assert (outcome.added, outcome.updated, outcome.unchanged) == (0, 0, 0)


def test_merge_requires_name() -> None:
    with pytest.raises(ValueError):
        merge_rows_by_name([], [{"DisplayName": "no name key"}])


# --- engine-driving importer -------------------------------------------------------------------


def test_import_rows_read_merge_refill_preserves_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = [
        {"Name": "keep", "Type": "quest", "DisplayName": "K"},
        {"Name": "old", "DisplayName": "Old"},
    ]
    table = _FakeTable(existing)
    fake = _FakeUnreal({"/Game/QuestTable": table})
    monkeypatch.setattr(importer_mod, "unreal", fake)

    result = importer_mod.import_rows(
        "/Game/QuestTable",
        [{"Name": "old", "DisplayName": "New"}, {"Name": "fresh", "DisplayName": "F"}],
    )

    assert result.ok
    assert (result.added, result.updated, result.unchanged) == (1, 1, 1)
    assert len(fake.DataTableFunctionLibrary.fill_calls) == 1
    _, json_string, struct = fake.DataTableFunctionLibrary.fill_calls[0]
    assert struct == "RowStruct-sentinel"  # table Row Struct passed for a headless import
    by_name = {r["Name"]: r for r in json.loads(json_string)}
    assert by_name["keep"] == {"Name": "keep", "Type": "quest", "DisplayName": "K"}  # verbatim
    assert by_name["old"]["DisplayName"] == "New"  # upserted
    assert by_name["fresh"]["DisplayName"] == "F"  # appended
    assert fake.EditorAssetLibrary.saved == [table]  # saved after a successful fill


def test_import_rows_surfaces_fill_false(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeUnreal({"/Game/T": _FakeTable([])})
    fake.DataTableFunctionLibrary.fill_return = False
    monkeypatch.setattr(importer_mod, "unreal", fake)

    result = importer_mod.import_rows("/Game/T", [{"Name": "a"}])
    assert not result.ok
    assert result.error == "fill_returned_false"
    assert "Row Struct" in result.message
    assert fake.EditorAssetLibrary.saved == []  # never saved when the fill bool is False


def test_import_rows_table_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer_mod, "unreal", _FakeUnreal({}))
    result = importer_mod.import_rows("/Game/Missing", [{"Name": "a"}])
    assert not result.ok and result.error == "asset_not_found"


def test_import_rows_no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer_mod, "unreal", None)
    result = importer_mod.import_rows("/Game/T", [{"Name": "a"}])
    assert not result.ok and result.error == "no_engine"


# --- bundle discovery + orchestration (reuses the real exporter output) ------------------------


def test_plan_discovers_datatables_and_excludes_preflight(tmp_path: Path) -> None:
    export_unreal_datatables(_bundle(), tmp_path)
    plan = owcopilot_bridge.plan_bundle_import(
        tmp_path, {"Quests": "/Game/Q", "Entities": "/Game/E", "Dialogues": "/Game/D"}
    )
    assert {t.file for t in plan.tables} == {"Quests.json", "Entities.json", "Dialogues.json"}
    assert all(t.mapped for t in plan.tables)
    quests = next(t for t in plan.tables if t.file == "Quests.json")
    assert quests.row_count == 2 and quests.table_path == "/Game/Q"


def test_plan_unmapped_tables_warn(tmp_path: Path) -> None:
    export_unreal_datatables(_bundle(), tmp_path)
    plan = owcopilot_bridge.plan_bundle_import(tmp_path)  # no table_map
    assert plan.tables and all(not t.mapped for t in plan.tables)
    assert any("Quests.json" in w for w in plan.warnings)


def test_import_bundle_drives_all_mapped_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_unreal_datatables(_bundle(), tmp_path)
    tables = {
        "/Game/QuestTable": _FakeTable([]),
        "/Game/EntityTable": _FakeTable([]),
        "/Game/DialogueTable": _FakeTable([]),
    }
    fake = _FakeUnreal(tables)
    monkeypatch.setattr(importer_mod, "unreal", fake)

    result = importer_mod.import_bundle(
        tmp_path,
        {
            "Quests": "/Game/QuestTable",
            "Entities": "/Game/EntityTable",
            "Dialogues": "/Game/DialogueTable",
        },
    )

    assert result.ok
    assert len(result.tables) == 3 and all(t.ok for t in result.tables)
    # preflight/csv/header were not treated as DataTables: exactly one fill per real table
    assert len(fake.DataTableFunctionLibrary.fill_calls) == 3
    assert {r["Name"] for r in tables["/Game/QuestTable"].rows} == {"quest_intro", "quest_patrol"}


def test_import_bundle_offline_is_structured_not_raised(tmp_path: Path) -> None:
    export_unreal_datatables(_bundle(), tmp_path)
    result = owcopilot_bridge.import_bundle(tmp_path, {"Quests": "/Game/Q"})  # no engine
    assert not result.ok
    assert result.tables and all(t.error == "no_engine" for t in result.tables)


# --- full-fidelity sub-tables push through the shape-driven path with no code change -----------


def _logic_bundle() -> ContentBundle:
    return ContentBundle(
        quests={
            "q_gate": Quest(
                id="q_gate",
                title="门后",
                stages=[QuestStage(id="s1", summary="谈话"), QuestStage(id="s2", summary="进门")],
                logic=QuestLogic(
                    stage_logic=[
                        StageLogic(
                            stage_id="s2",
                            precondition="helped",
                            effects_on_complete=[Effect(var="helped", op="set", value=True)],
                        )
                    ]
                ),
            )
        },
        dialogue_trees={
            "dlg_gate": DialogueTree(
                id="dlg_gate",
                root_node="n0",
                nodes={
                    "n0": DialogueNode(
                        id="n0",
                        choices=[DialogueChoice(text="ok", next_node="n_end")],
                    ),
                    "n_end": DialogueNode(id="n_end", text="……"),
                },
            )
        },
    )


def test_plan_discovers_new_fidelity_tables(tmp_path: Path) -> None:
    """The new logic/stage/dialogue-tree DataTables are discovered by the same shape rule as the
    original three — no filename hard-coding, so the push path needs no change."""
    export_unreal_datatables(_logic_bundle(), tmp_path)
    plan = owcopilot_bridge.plan_bundle_import(tmp_path)  # discovery is shape-driven, no map needed
    discovered = {t.file for t in plan.tables}
    # this bundle carries stages, stage logic and a dialogue tree (no vars/branches/rewards), so
    # exactly those sub-tables are emitted and discovered by the shape rule.
    assert {
        "QuestStages.json",
        "DialogueTrees.json",
        "DialogueTreeNodes.json",
        "DialogueTreeChoices.json",
    } <= discovered
    # preflight/header/csv are not DataTables and must not be discovered
    assert "unreal_preflight.json" not in discovered


def test_import_bundle_drives_new_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    export_unreal_datatables(_logic_bundle(), tmp_path)
    targets = {
        "Quests": "/Game/Quests",
        "QuestStages": "/Game/QuestStages",
        "DialogueTreeChoices": "/Game/DialogueTreeChoices",
    }
    tables = {path: _FakeTable([]) for path in targets.values()}
    fake = _FakeUnreal(tables)
    monkeypatch.setattr(importer_mod, "unreal", fake)

    result = importer_mod.import_bundle(tmp_path, targets)
    assert result.ok and all(t.ok for t in result.tables)
    # the new sub-tables were filled just like the originals (one fill per mapped table)
    stage_names = {r["Name"] for r in tables["/Game/QuestStages"].rows}
    assert stage_names == {"q_gate::s1", "q_gate::s2"}
    choice_names = {r["Name"] for r in tables["/Game/DialogueTreeChoices"].rows}
    assert choice_names == {"dlg_gate::n0::choice::0"}
