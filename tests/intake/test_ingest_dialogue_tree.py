"""对白树的表格导入路径（契约见 src/owcopilot/content/normalize.py）。

本文件守的核心断言是：**一张真实策划配表导进来之后，全套既有确定性审计规则立刻对它生效**。
在这条路径存在之前，DialogueTree 只能由前端手编或 AI 生成过人审产生——审计只能审工具自己造的树。

测试组装 CSV/行对象、驱动真实的 normalize / ingest / 审计，不重写任何组装逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.content.importers.base import RawObject
from owcopilot.content.ingest import ingest_paths, parse_paths
from owcopilot.content.models import (
    ContentBundle,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
)
from owcopilot.content.normalize import normalize_raw_objects
from owcopilot.content.store import ContentStore

_GOLDEN_TREE = Path(__file__).parent / "fixtures" / "dialogue_tree.json"

_HEAD = {"id": "dlg_t", "title": "T", "root_node": "n1"}

# 契约文档里的列名，逐个写成字面量：写错一个，下面的导入就会失败，所以这张表同时是契约的
# 可执行说明。列名本身另有一组 _row() 用例独立钉住（那些用例直接以列名为关键字构造行）。
_COLUMNS = [
    "kind",
    "tree_id",
    "id",
    "title",
    "quest_id",
    "root_node",
    "participants",
    "tags",
    "speaker",
    "text",
    "text_key",
    "next_node",
    "from_node",
    "condition",
    "effects",
]


def _write_table_csv(tree: dict, path: Path) -> Path:
    """把一棵真实的对白树反写成契约里那张表。

    这不是 round-trip 自证：列名是本文件里的字面量而非从实现里取的常量，所以导入器必须真的认
    这些列；这条用例守的是**一棵独立构造的树（4 节点 / 3 选项 / Unicode 正文 / 条件 / 后果）
    经表格往返后不掉数据**。
    """
    import csv as csv_module

    rows: list[dict[str, object]] = [
        {
            "kind": "dialogue_tree",
            "id": tree["id"],
            "title": tree.get("title", ""),
            "quest_id": tree.get("quest_id") or "",
            "root_node": tree.get("root_node", ""),
            "participants": ";".join(tree.get("participants", [])),
            "tags": ";".join(tree.get("tags", [])),
        }
    ]
    for node_id, node in tree["nodes"].items():
        rows.append(
            {
                "kind": "dialogue_tree_node",
                "tree_id": tree["id"],
                "id": node_id,
                "speaker": node.get("speaker_id") or "",
                "text": node.get("text", ""),
                "text_key": node.get("text_key", ""),
                "next_node": node.get("next_node") or "",
            }
        )
        for choice in node.get("choices", []):
            rows.append(
                {
                    "kind": "dialogue_tree_choice",
                    "tree_id": tree["id"],
                    "from_node": node_id,
                    "text": choice.get("text", ""),
                    "text_key": choice.get("text_key", ""),
                    "next_node": choice.get("next_node") or "",
                    "condition": choice.get("condition", ""),
                    "effects": (
                        json.dumps(choice["effects"], ensure_ascii=False)
                        if choice.get("effects")
                        else ""
                    ),
                }
            )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv_module.DictWriter(handle, fieldnames=_COLUMNS, lineterminator="\n", restval="")
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def golden_csv(tmp_path: Path) -> Path:
    stored = json.loads(_GOLDEN_TREE.read_text(encoding="utf-8"))
    return _write_table_csv(stored, tmp_path / "dialogue_tree_table.csv")


def _row(kind: str, **data: object) -> RawObject:
    return RawObject(kind=kind, data={"kind": kind, **data}, source_path="rows.csv", row=1)


def _bundle(*rows: RawObject) -> ContentBundle:
    return normalize_raw_objects(list(rows))


def _issue_codes(bundle: ContentBundle) -> set[str]:
    ctx = AuditContext.from_bundle(bundle)
    return {
        issue.rule_code
        for rule in build_default_rule_registry().enabled()
        for issue in rule.check(ctx)
    }


# --- 金对照：反写的 CSV 导入后必须与独立测试夹具等价 --------------------------------------


def test_reverse_written_csv_reproduces_the_stored_dialogue_tree(golden_csv: Path) -> None:
    bundle = normalize_raw_objects(parse_paths([golden_csv]))
    stored = json.loads(_GOLDEN_TREE.read_text(encoding="utf-8"))

    tree = bundle.dialogue_trees["tree_fixture"]
    assert tree.title == stored["title"]
    assert tree.quest_id == stored["quest_id"]
    assert tree.root_node == stored["root_node"]
    assert tree.participants == stored["participants"]
    assert tree.tags == stored["tags"]
    assert set(tree.nodes) == set(stored["nodes"])

    for node_id, stored_node in stored["nodes"].items():
        node = tree.nodes[node_id]
        assert node.speaker_id == stored_node.get("speaker_id")
        assert node.text == stored_node.get("text", "")
        assert node.text_key == stored_node.get("text_key", "")
        assert node.next_node == stored_node.get("next_node")
        stored_choices = stored_node.get("choices", [])
        assert len(node.choices) == len(stored_choices)
        # 选项顺序 = 行顺序，必须逐位对齐而不是当集合比
        for choice, stored_choice in zip(node.choices, stored_choices, strict=True):
            assert choice.text == stored_choice["text"]
            assert choice.text_key == stored_choice.get("text_key", "")
            assert choice.next_node == stored_choice.get("next_node")
            assert choice.condition == stored_choice.get("condition", "")
            assert [e.model_dump() for e in choice.effects] == [
                {"var": e["var"], "op": e.get("op", "set"), "value": e["value"]}
                for e in stored_choice.get("effects", [])
            ]


def test_imported_tree_carries_provenance_back_to_the_spreadsheet_row(golden_csv: Path) -> None:
    bundle = normalize_raw_objects(parse_paths([golden_csv]))

    source = bundle.dialogue_trees["tree_fixture"].source_ref
    assert source is not None
    assert source.path.endswith("dialogue_tree_table.csv")
    assert source.row is not None


# --- 宽容导入 + 审计裁决：结构问题放行进来，由既有规则抓 ----------------------------------------


def test_a_broken_link_imports_and_is_caught_by_the_existing_audit_rule() -> None:
    bundle = _bundle(
        _row("dialogue_tree", **_HEAD),
        _row("dialogue_tree_node", tree_id="dlg_t", id="n1", text="hi", next_node="nowhere"),
    )

    assert "n1" in bundle.dialogue_trees["dlg_t"].nodes  # 导入成功，没被挡在门外
    assert "DIALOGUE_TREE_BROKEN_LINK" in _issue_codes(bundle)


def test_a_condition_on_an_undeclared_variable_imports_and_is_caught() -> None:
    """B 线的核心价值断言：策划表里的条件表达式一进来，既有的变量声明检查立刻生效。

    该规则要拿绑定任务的变量表当尺子，所以这里给出一个只声明了 ``declared`` 的任务；表里那条
    条件读的是 ``never_declared``。"""
    bundle = _bundle(
        _row("dialogue_tree", **{**_HEAD, "quest_id": "q_host"}),
        _row("dialogue_tree_node", tree_id="dlg_t", id="n1", text="hi"),
        _row(
            "dialogue_tree_choice",
            tree_id="dlg_t",
            from_node="n1",
            text="选它",
            condition="never_declared == true",
        ),
    )
    bundle.quests["q_host"] = Quest(
        id="q_host",
        title="宿主任务",
        stages=[QuestStage(id="s0", summary="")],
        logic=QuestLogic(
            variables=[LogicVar(id="declared", type=LogicVarType.BOOL, default=False)]
        ),
    )

    assert bundle.dialogue_trees["dlg_t"].nodes["n1"].choices[0].condition
    assert "DIALOGUE_CONDITION_UNDEFINED_VAR" in _issue_codes(bundle)


def test_a_line_without_a_localization_key_imports_and_is_caught() -> None:
    bundle = _bundle(
        _row("dialogue_tree", **_HEAD),
        _row("dialogue_tree_node", tree_id="dlg_t", id="n1", text="有正文没有键"),
    )

    assert "DIALOGUE_TREE_MISSING_TEXT_KEY" in _issue_codes(bundle)


# --- 导入期只拦身份问题 -------------------------------------------------------------------------


def test_a_node_row_pointing_at_an_unknown_tree_is_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        _bundle(
            _row("dialogue_tree", **_HEAD),
            _row("dialogue_tree_node", tree_id="dlg_missing", id="n1"),
        )

    assert "dlg_missing" in str(excinfo.value)
    assert "头行" in str(excinfo.value)  # 引导下一步


def test_a_duplicate_node_id_within_one_tree_is_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        _bundle(
            _row("dialogue_tree", **_HEAD),
            _row("dialogue_tree_node", tree_id="dlg_t", id="n1"),
            _row("dialogue_tree_node", tree_id="dlg_t", id="n1"),
        )

    assert "n1" in str(excinfo.value)


def test_a_choice_pointing_at_an_unknown_node_is_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        _bundle(
            _row("dialogue_tree", **_HEAD),
            _row("dialogue_tree_node", tree_id="dlg_t", id="n1"),
            _row("dialogue_tree_choice", tree_id="dlg_t", from_node="ghost", text="x"),
        )

    assert "ghost" in str(excinfo.value)


def test_malformed_effects_json_is_rejected_with_the_row_named() -> None:
    with pytest.raises(ValueError) as excinfo:
        _bundle(
            _row("dialogue_tree", **_HEAD),
            _row("dialogue_tree_node", tree_id="dlg_t", id="n1"),
            _row(
                "dialogue_tree_choice",
                tree_id="dlg_t",
                from_node="n1",
                text="x",
                effects="{不是数组}",
            ),
        )

    message = str(excinfo.value)
    assert "n1" in message and "JSON" in message
    assert '"var"' in message  # 给出正确写法，而不是甩一句解析失败


def test_effects_round_trip_through_the_json_cell() -> None:
    bundle = _bundle(
        _row("dialogue_tree", **_HEAD),
        _row("dialogue_tree_node", tree_id="dlg_t", id="n1"),
        _row(
            "dialogue_tree_choice",
            tree_id="dlg_t",
            from_node="n1",
            text="x",
            effects='[{"var": "favor", "op": "inc", "value": 2}]',
        ),
    )

    effect = bundle.dialogue_trees["dlg_t"].nodes["n1"].choices[0].effects[0]
    assert (effect.var, effect.op, effect.value) == ("favor", "inc", 2)


# --- 行序与多选项 -------------------------------------------------------------------------------


def test_choice_order_follows_row_order() -> None:
    bundle = _bundle(
        _row("dialogue_tree", **_HEAD),
        _row("dialogue_tree_node", tree_id="dlg_t", id="n1"),
        _row("dialogue_tree_choice", tree_id="dlg_t", from_node="n1", text="第一"),
        _row("dialogue_tree_choice", tree_id="dlg_t", from_node="n1", text="第二"),
    )

    assert [c.text for c in bundle.dialogue_trees["dlg_t"].nodes["n1"].choices] == ["第一", "第二"]


def test_a_choice_row_may_precede_its_node_row_in_the_file() -> None:
    """两遍组装：选项行写在节点行前面也要能挂上，策划的表不保证有序。"""
    bundle = _bundle(
        _row("dialogue_tree", **_HEAD),
        _row("dialogue_tree_choice", tree_id="dlg_t", from_node="n1", text="先写的选项"),
        _row("dialogue_tree_node", tree_id="dlg_t", id="n1"),
    )

    assert [c.text for c in bundle.dialogue_trees["dlg_t"].nodes["n1"].choices] == ["先写的选项"]


# --- 端到端：经 ingest 落库 ---------------------------------------------------------------------


def test_ingest_reports_dialogue_tree_changes_and_writes_them(
    tmp_path: Path, golden_csv: Path
) -> None:
    store = ContentStore(tmp_path / "content")
    store.save(ContentBundle())

    result = ingest_paths([golden_csv], store=store, dry_run=False)

    assert any(change.object_type == "dialogue_tree" for change in result.changes)
    assert "tree_fixture" in store.load().dialogue_trees


def test_the_same_table_imports_identically_from_xlsx(tmp_path: Path, golden_csv: Path) -> None:
    """策划的表更常是 xlsx 而不是 csv；两条路径必须落到同一棵树。"""
    import csv as csv_module

    from openpyxl import Workbook

    with golden_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv_module.reader(handle))
    workbook = Workbook()
    for row in rows:
        workbook.active.append(row)
    xlsx_path = tmp_path / "dialogue_tree_table.xlsx"
    workbook.save(xlsx_path)

    from_csv = normalize_raw_objects(parse_paths([golden_csv])).dialogue_trees
    from_xlsx = normalize_raw_objects(parse_paths([xlsx_path])).dialogue_trees

    assert from_csv["tree_fixture"].nodes == from_xlsx["tree_fixture"].nodes


def test_importing_the_same_table_twice_is_idempotent(tmp_path: Path, golden_csv: Path) -> None:
    store = ContentStore(tmp_path / "content")
    store.save(ContentBundle())
    ingest_paths([golden_csv], store=store, dry_run=False)
    after_first = store.load()

    ingest_paths([golden_csv], store=store, dry_run=False)
    after_second = store.load()

    assert after_first.dialogue_trees == after_second.dialogue_trees
