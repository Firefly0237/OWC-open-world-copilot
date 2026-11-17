"""世界级 flag 交叉引用（logic/xref.py）。

Tests assemble bundles from variables and assert the cross-reference production code returns;
no test re-implements the traversal it is checking.
"""

from __future__ import annotations

from owcopilot.content.models import (
    Branch,
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.logic.xref import FlagScope, SiteRole, build_flag_xref


def _quest(quest_id: str, stages: list[str], logic: QuestLogic | None) -> Quest:
    return Quest(
        id=quest_id,
        title=quest_id,
        stages=[QuestStage(id=s, summary="") for s in stages],
        logic=logic,
    )


def _writer_quest() -> Quest:
    """Declares ``shared`` and writes it when ``s_talk`` completes."""
    return _quest(
        "q_writer",
        ["s_talk"],
        QuestLogic(
            variables=[LogicVar(id="shared", type=LogicVarType.BOOL, default=False)],
            stage_logic=[
                StageLogic(
                    stage_id="s_talk",
                    effects_on_complete=[Effect(var="shared", op="set", value=True)],
                )
            ],
        ),
    )


def _reader_quest() -> Quest:
    """Declares ``shared`` too and gates a branch on it — the cross-quest sharing pattern."""
    return _quest(
        "q_reader",
        ["s_act"],
        QuestLogic(
            variables=[LogicVar(id="shared", type=LogicVarType.BOOL, default=False)],
            branches=[
                Branch(id="b_go", from_stage="s_act", condition="shared == true", outcome="done")
            ],
        ),
    )


# --- 基本收集 ---------------------------------------------------------------------------------


def test_cross_quest_shared_flag_records_declarations_writers_and_readers() -> None:
    bundle = ContentBundle(quests={"q_writer": _writer_quest(), "q_reader": _reader_quest()})

    xref = build_flag_xref(bundle)
    facts = xref.facts["shared"]

    assert facts.scope is FlagScope.DECLARED
    assert sorted(d.quest_id for d in facts.declarations) == ["q_reader", "q_writer"]
    assert [(w.owner_id, w.field_path) for w in facts.writers] == [
        ("q_writer", "logic/stage_logic/0/effects_on_complete/0")
    ]
    assert [(r.owner_id, r.field_path) for r in facts.readers] == [
        ("q_reader", "logic/branches/0/condition")
    ]
    assert facts.writers[0].role is SiteRole.WRITE
    assert facts.readers[0].role is SiteRole.READ


def test_quest_level_precondition_counts_as_a_reader() -> None:
    bundle = ContentBundle(
        quests={
            "q_writer": _writer_quest(),
            "q_gated": _quest(
                "q_gated",
                ["s0"],
                QuestLogic(
                    variables=[LogicVar(id="shared", type=LogicVarType.BOOL, default=False)],
                    precondition="shared == true",
                ),
            ),
        }
    )

    readers = build_flag_xref(bundle).facts["shared"].readers
    assert ("q_gated", "logic/precondition") in [(r.owner_id, r.field_path) for r in readers]


def test_dialogue_choice_conditions_and_effects_are_traced_with_node_paths() -> None:
    tree = DialogueTree(
        id="dlg_a",
        quest_id="q_reader",
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                choices=[
                    DialogueChoice(
                        text="说了",
                        condition="shared == true",
                        effects=[Effect(var="mood", op="inc", value=1)],
                    )
                ],
            )
        },
    )
    bundle = ContentBundle(quests={"q_reader": _reader_quest()}, dialogue_trees={"dlg_a": tree})

    xref = build_flag_xref(bundle)

    shared_readers = [(r.owner_id, r.field_path) for r in xref.facts["shared"].readers]
    assert ("dlg_a", "nodes/n1/choices/0/condition") in shared_readers
    mood_writers = [(w.owner_id, w.field_path) for w in xref.facts["mood"].writers]
    assert mood_writers == [("dlg_a", "nodes/n1/choices/0/effects/0")]
    # 对白树经 quest_id 绑定，站点带上它所属的任务作为声明作用域
    assert xref.facts["shared"].readers[-1].quest_id == "q_reader"


def test_branch_effects_are_traced() -> None:
    quest = _quest(
        "q_branch",
        ["s0"],
        QuestLogic(
            variables=[LogicVar(id="rage", type=LogicVarType.INT, default=0)],
            branches=[
                Branch(
                    id="b0",
                    from_stage="s0",
                    outcome="end",
                    effects=[Effect(var="rage", op="inc", value=1)],
                )
            ],
        ),
    )

    writers = build_flag_xref(ContentBundle(quests={"q_branch": quest})).facts["rage"].writers
    assert [(w.owner_id, w.field_path) for w in writers] == [
        ("q_branch", "logic/branches/0/effects/0")
    ]


def test_quest_without_logic_contributes_only_its_completion_flag() -> None:
    bundle = ContentBundle(quests={"q_plain": _quest("q_plain", ["s0"], None)})

    xref = build_flag_xref(bundle)
    assert set(xref.facts) == {"quest:q_plain.done"}


# --- 声明冲突 ---------------------------------------------------------------------------------


def test_declaration_conflict_is_reported_when_type_or_default_disagree() -> None:
    writer = _writer_quest()
    reader = _reader_quest()
    reader.logic.variables[0] = LogicVar(id="shared", type=LogicVarType.INT, default=3)  # type: ignore[union-attr]

    conflicts = build_flag_xref(
        ContentBundle(quests={"q_writer": writer, "q_reader": reader})
    ).declaration_conflicts()

    assert [f.flag for f in conflicts] == ["shared"]


def test_identical_declarations_in_two_quests_are_not_a_conflict() -> None:
    """误报边界：同一个 flag 在两个任务里声明成完全一样的形状是正常的共享写法，不是冲突。"""
    bundle = ContentBundle(quests={"q_writer": _writer_quest(), "q_reader": _reader_quest()})

    assert build_flag_xref(bundle).declaration_conflicts() == []


def test_bool_false_and_int_zero_declarations_are_a_conflict() -> None:
    """误报边界的反面：Python 里 False == 0，形状比较必须把它们分开，否则冲突会被吃掉。"""
    writer = _writer_quest()
    reader = _reader_quest()
    reader.logic.variables[0] = LogicVar(id="shared", type=LogicVarType.INT, default=0)  # type: ignore[union-attr]

    conflicts = build_flag_xref(
        ContentBundle(quests={"q_writer": writer, "q_reader": reader})
    ).declaration_conflicts()

    assert [f.flag for f in conflicts] == ["shared"]


# --- 安全阀：只对声明过的 flag 下结论 -----------------------------------------------------------


def test_provably_never_written_covers_a_declared_flag_nobody_writes() -> None:
    bundle = ContentBundle(quests={"q_reader": _reader_quest()})

    assert "shared" in build_flag_xref(bundle).provably_never_written()


def test_declared_flag_written_anywhere_in_the_world_is_not_provably_never_written() -> None:
    bundle = ContentBundle(quests={"q_writer": _writer_quest(), "q_reader": _reader_quest()})

    assert "shared" not in build_flag_xref(bundle).provably_never_written()


def test_undeclared_reference_never_gets_a_never_written_verdict() -> None:
    """安全阀：没在任何任务变量表里声明的裸引用，可能由引擎侧 gameplay 写，工具看不见。"""
    quest = _quest(
        "q0",
        ["s0"],
        QuestLogic(
            branches=[Branch(id="b0", from_stage="s0", condition="engine_flag", outcome="e")]
        ),
    )

    xref = build_flag_xref(ContentBundle(quests={"q0": quest}))
    assert xref.facts["engine_flag"].scope is FlagScope.UNDECLARED
    assert "engine_flag" not in xref.provably_never_written()


def test_quest_state_and_faction_reputation_refs_are_implicit_scope() -> None:
    quest = _quest(
        "q0",
        ["s0"],
        QuestLogic(
            branches=[
                Branch(
                    id="b0",
                    from_stage="s0",
                    condition="quest:q_other.done == true and rep:fac_iron > 2",
                    outcome="e",
                )
            ]
        ),
    )

    xref = build_flag_xref(ContentBundle(quests={"q0": quest}))
    assert xref.facts["quest:q_other.done"].scope is FlagScope.QUEST_STATE
    assert xref.facts["rep:fac_iron"].scope is FlagScope.FACTION_REP
    assert xref.provably_never_written() == frozenset()


def test_a_quest_writes_its_own_completion_flag() -> None:
    bundle = ContentBundle(quests={"q_writer": _writer_quest()})

    writers = build_flag_xref(bundle).facts["quest:q_writer.done"].writers
    assert [w.owner_id for w in writers] == ["q_writer"]


# --- 读侧诚实性 -------------------------------------------------------------------------------


def test_unparsable_guard_is_recorded_rather_than_silently_dropped() -> None:
    quest = _quest(
        "q0",
        ["s0"],
        QuestLogic(
            variables=[LogicVar(id="a", type=LogicVarType.BOOL, default=False)],
            branches=[Branch(id="b0", from_stage="s0", condition="a ==== ", outcome="e")],
        ),
    )

    xref = build_flag_xref(ContentBundle(quests={"q0": quest}))

    assert [u.field_path for u in xref.unparsable] == ["logic/branches/0/condition"]
    assert xref.reads_complete is False


def test_never_read_is_withheld_while_any_guard_is_unparsable() -> None:
    """写了没人读的结论建立在『读取方已收全』上；只要有一条表达式没解析成功就不能下这个结论。"""
    quest = _quest(
        "q0",
        ["s0"],
        QuestLogic(
            variables=[LogicVar(id="written_only", type=LogicVarType.BOOL, default=False)],
            stage_logic=[
                StageLogic(
                    stage_id="s0",
                    effects_on_complete=[Effect(var="written_only", op="set", value=True)],
                )
            ],
            branches=[Branch(id="b0", from_stage="s0", condition="a ==== ", outcome="e")],
        ),
    )

    xref = build_flag_xref(ContentBundle(quests={"q0": quest}))
    assert xref.never_read() == frozenset()

    quest.logic.branches[0].condition = ""  # type: ignore[union-attr]
    healthy = build_flag_xref(ContentBundle(quests={"q0": quest}))
    assert healthy.reads_complete is True
    assert healthy.never_read() == frozenset({"written_only"})


def test_empty_expressions_are_not_treated_as_parse_failures() -> None:
    bundle = ContentBundle(quests={"q_writer": _writer_quest()})

    xref = build_flag_xref(bundle)
    assert xref.unparsable == []
    assert xref.reads_complete is True


# --- 确定性与空输入 ---------------------------------------------------------------------------


def test_empty_bundle_yields_an_empty_cross_reference() -> None:
    xref = build_flag_xref(ContentBundle())

    assert xref.facts == {}
    assert xref.provably_never_written() == frozenset()
    assert xref.never_read() == frozenset()


def test_cross_reference_is_deterministic() -> None:
    bundle = ContentBundle(quests={"q_writer": _writer_quest(), "q_reader": _reader_quest()})

    first = build_flag_xref(bundle).model_dump_json()
    second = build_flag_xref(bundle).model_dump_json()
    assert first == second


# --- 与既有单任务检查器的口径一致（防两份真相）-------------------------------------------------


def test_quest_scoped_writer_set_matches_the_temporal_checkers_own_notion() -> None:
    """xref 推出的『这个任务写了哪些 flag』必须与 temporal 私有实现一致，否则世界层与任务层
    会各说各话。"""
    from owcopilot.logic.temporal import _written_flags

    quest = _writer_quest()
    xref = build_flag_xref(ContentBundle(quests={quest.id: quest}))

    from_xref = {
        flag
        for flag, facts in xref.facts.items()
        if any(w.owner_kind == "quest" and w.owner_id == quest.id for w in facts.writers)
        and not flag.startswith("quest:")
    }
    assert from_xref == _written_flags(quest.logic)  # type: ignore[arg-type]
