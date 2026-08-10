"""世界级 flag 一致性规则（audit/rules/flag_rules.py）的判定与误报边界。

测试只组装 bundle、断言规则产出的 Issue，不重写任何判定逻辑。
"""

from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.models import Issue, Severity
from owcopilot.audit.rules.flag_rules import WorldFlagConsistencyRule
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
from owcopilot.evaluation.acceptance import build_acceptance_world

_RULE = WorldFlagConsistencyRule()


def _issues(bundle: ContentBundle) -> list[Issue]:
    return list(_RULE.check(AuditContext.from_bundle(bundle)))


def _subcodes(bundle: ContentBundle) -> list[str]:
    return sorted(issue.message.split("]")[0].lstrip("[") for issue in _issues(bundle))


def _quest(
    quest_id: str,
    logic: QuestLogic | None,
    *,
    stages: list[str] | None = None,
    timeline_order: int | None = None,
) -> Quest:
    return Quest(
        id=quest_id,
        title=quest_id,
        timeline_order=timeline_order,
        stages=[QuestStage(id=s, summary="") for s in (stages or ["s0"])],
        logic=logic,
    )


def _declares(flag: str, var_type: LogicVarType, default: bool | int | str) -> list[LogicVar]:
    return [LogicVar(id=flag, type=var_type, default=default)]


def _writes(flag: str) -> QuestLogic:
    return QuestLogic(
        variables=_declares(flag, LogicVarType.BOOL, False),
        stage_logic=[
            StageLogic(stage_id="s0", effects_on_complete=[Effect(var=flag, op="set", value=True)])
        ],
    )


def _reads(flag: str) -> QuestLogic:
    return QuestLogic(
        variables=_declares(flag, LogicVarType.BOOL, False),
        branches=[Branch(id="b0", from_stage="s0", condition=f"{flag} == true", outcome="done")],
    )


# --- FLAG_DECL_CONFLICT ------------------------------------------------------------------------


def test_declaration_conflict_is_reported_when_two_quests_disagree() -> None:
    a = _quest("q_a", _writes("shared"))
    b = _quest("q_b", _reads("shared"))
    b.logic.variables[0] = LogicVar(id="shared", type=LogicVarType.INT, default=3)  # type: ignore[union-attr]

    assert "FLAG_DECL_CONFLICT" in _subcodes(ContentBundle(quests={"q_a": a, "q_b": b}))


def test_identical_declarations_in_two_quests_are_not_reported() -> None:
    """误报边界：同一个 flag 在两个任务里声明成一模一样的形状是正常的共享写法。"""
    bundle = ContentBundle(
        quests={"q_a": _quest("q_a", _writes("shared")), "q_b": _quest("q_b", _reads("shared"))}
    )

    assert "FLAG_DECL_CONFLICT" not in _subcodes(bundle)


def test_declaration_conflict_evidence_points_at_every_declaring_quest() -> None:
    a = _quest("q_a", _writes("shared"))
    b = _quest("q_b", _reads("shared"))
    b.logic.variables[0] = LogicVar(id="shared", type=LogicVarType.INT, default=3)  # type: ignore[union-attr]

    issue = next(
        i for i in _issues(ContentBundle(quests={"q_a": a, "q_b": b})) if "CONFLICT" in i.message
    )

    assert {e.target_ref for e in issue.evidence} == {"quest:q_a", "quest:q_b"}
    assert all(e.path.startswith("logic/variables/") for e in issue.evidence)


# --- FLAG_NEVER_WRITTEN ------------------------------------------------------------------------


def test_gate_on_a_flag_nobody_writes_is_reported() -> None:
    bundle = ContentBundle(quests={"q_b": _quest("q_b", _reads("orphan"))})

    assert "FLAG_NEVER_WRITTEN" in _subcodes(bundle)


def test_gate_is_not_reported_when_some_other_quest_writes_the_flag() -> None:
    """误报边界：写入方在别的任务里，也算有人写。"""
    bundle = ContentBundle(
        quests={"q_a": _quest("q_a", _writes("shared")), "q_b": _quest("q_b", _reads("shared"))}
    )

    assert "FLAG_NEVER_WRITTEN" not in _subcodes(bundle)


def test_gate_is_not_reported_when_a_dialogue_choice_writes_the_flag() -> None:
    """误报边界：对白选项的后果也是写入方——这是最常见的跨层写法。"""
    tree = DialogueTree(
        id="dlg",
        quest_id="q_b",
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                choices=[
                    DialogueChoice(
                        text="做了", effects=[Effect(var="shared", op="set", value=True)]
                    )
                ],
            )
        },
    )
    bundle = ContentBundle(
        quests={"q_b": _quest("q_b", _reads("shared"))}, dialogue_trees={"dlg": tree}
    )

    assert "FLAG_NEVER_WRITTEN" not in _subcodes(bundle)


def test_undeclared_reference_is_never_reported_as_unwritten() -> None:
    """安全阀：没在任何变量表里声明的裸引用可能由引擎侧写，工具看不见，不能判。"""
    quest = _quest(
        "q0",
        QuestLogic(
            branches=[Branch(id="b0", from_stage="s0", condition="engine_flag", outcome="done")]
        ),
    )

    assert _subcodes(ContentBundle(quests={"q0": quest})) == []


def test_a_declared_flag_with_neither_readers_nor_writers_is_not_reported() -> None:
    """误报边界：声明了但完全没用的变量只是冗余，不是坏掉的门槛，不占一条问题。"""
    quest = _quest("q0", QuestLogic(variables=_declares("unused", LogicVarType.BOOL, False)))

    assert _subcodes(ContentBundle(quests={"q0": quest})) == []


# --- FLAG_NEVER_READ ---------------------------------------------------------------------------


def test_flag_written_but_never_read_is_reported() -> None:
    bundle = ContentBundle(quests={"q_a": _quest("q_a", _writes("counter"))})

    assert "FLAG_NEVER_READ" in _subcodes(bundle)


def test_flag_written_and_read_is_not_reported() -> None:
    bundle = ContentBundle(
        quests={"q_a": _quest("q_a", _writes("shared")), "q_b": _quest("q_b", _reads("shared"))}
    )

    assert "FLAG_NEVER_READ" not in _subcodes(bundle)


def test_never_read_is_withheld_while_any_expression_in_the_world_is_unparsable() -> None:
    """安全阀：读取方收不全的时候，『没人读它』就是猜的，整条判定让位。"""
    writer = _quest("q_a", _writes("counter"))
    broken = _quest(
        "q_b",
        QuestLogic(
            variables=_declares("other", LogicVarType.BOOL, False),
            branches=[Branch(id="b0", from_stage="s0", condition="other ==== ", outcome="done")],
        ),
    )

    healthy_only = ContentBundle(quests={"q_a": writer})
    assert "FLAG_NEVER_READ" in _subcodes(healthy_only)

    with_broken = ContentBundle(quests={"q_a": writer, "q_b": broken})
    assert "FLAG_NEVER_READ" not in _subcodes(with_broken)


# --- PHASE_REFERENCE_TOO_EARLY -----------------------------------------------------------------


def test_reading_a_flag_only_later_content_writes_is_reported() -> None:
    early = _quest("q_early", _reads("later_flag"), timeline_order=1)
    late = _quest("q_late", _writes("later_flag"), timeline_order=5)

    assert "PHASE_REFERENCE_TOO_EARLY" in _subcodes(
        ContentBundle(quests={"q_early": early, "q_late": late})
    )


def test_same_timeline_position_is_not_too_early() -> None:
    """误报边界：同一时间线位置的两条内容之间没有先后可言。"""
    a = _quest("q_a", _reads("shared"), timeline_order=3)
    b = _quest("q_b", _writes("shared"), timeline_order=3)

    assert "PHASE_REFERENCE_TOO_EARLY" not in _subcodes(ContentBundle(quests={"q_a": a, "q_b": b}))


def test_reader_without_a_timeline_position_is_skipped() -> None:
    """误报边界：没排序号就无从比较，保守跳过。"""
    a = _quest("q_a", _reads("shared"), timeline_order=None)
    b = _quest("q_b", _writes("shared"), timeline_order=5)

    assert "PHASE_REFERENCE_TOO_EARLY" not in _subcodes(ContentBundle(quests={"q_a": a, "q_b": b}))


def test_one_writer_without_a_timeline_position_disables_the_comparison() -> None:
    """误报边界：只要有一个写入方没排序号，『最早的写入在哪』就不成立，整条跳过。"""
    reader = _quest("q_r", _reads("shared"), timeline_order=1)
    ordered = _quest("q_w1", _writes("shared"), timeline_order=5)
    unordered = _quest("q_w2", _writes("shared"), timeline_order=None)

    assert "PHASE_REFERENCE_TOO_EARLY" not in _subcodes(
        ContentBundle(quests={"q_r": reader, "q_w1": ordered, "q_w2": unordered})
    )


def test_a_dialogue_tree_inherits_the_timeline_position_of_the_quest_it_is_bound_to() -> None:
    tree = DialogueTree(
        id="dlg",
        quest_id="q_early",
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1", choices=[DialogueChoice(text="问", condition="later_flag == true")]
            )
        },
    )
    bundle = ContentBundle(
        quests={
            "q_early": _quest("q_early", _reads("later_flag"), timeline_order=1),
            "q_late": _quest("q_late", _writes("later_flag"), timeline_order=5),
        },
        dialogue_trees={"dlg": tree},
    )

    too_early = [i for i in _issues(bundle) if "TOO_EARLY" in i.message]
    assert any(
        any(e.target_ref == "dialogue_tree:dlg" for e in issue.evidence) for issue in too_early
    )


# --- 门禁 -------------------------------------------------------------------------------------


def test_all_issues_are_warnings_not_release_blockers() -> None:
    """本轮四个子判定一律 WARNING：ERROR 等于新增一条发布阻断条件，那是另一个决定。"""
    bundle = ContentBundle(quests={"q_a": _quest("q_a", _writes("counter"))})

    assert {i.severity for i in _issues(bundle)} == {Severity.WARNING}


def test_clean_acceptance_world_produces_no_findings() -> None:
    assert _issues(build_acceptance_world()) == []
