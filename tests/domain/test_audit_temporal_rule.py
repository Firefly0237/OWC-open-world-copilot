# FLAG_TEMPORAL_INVARIANT 审计规则专属测试：软锁经 runner 报出 + 规约违反 + 空泛/畸形降级 + 零误报。
"""``FlagTemporalInvariantRule`` tests — proving the sliced-CTL checker is genuinely wired into the
deterministic audit runner (A1), not an engine that no one calls.

Tests assemble bundles and assert on the production ``AuditRunner`` +
``build_default_rule_registry`` path; the temporal verdicts come from the real engine, not re-run.
"""

from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.models import Severity
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import (
    Branch,
    ContentBundle,
    Effect,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)

_RULE = "FLAG_TEMPORAL_INVARIANT"


def _run(bundle: ContentBundle):
    return AuditRunner(build_default_rule_registry()).run(AuditContext.from_bundle(bundle))


def _temporal_issues(bundle: ContentBundle):
    return [i for i in _run(bundle).issues if i.rule_code == _RULE]


def _bundle_with(quest: Quest) -> ContentBundle:
    bundle = ContentBundle()
    bundle.quests[quest.id] = quest
    return bundle


def _softlock_quest() -> Quest:
    return Quest(
        id="q_softlock", title="t",
        stages=[QuestStage(id=f"s{i}", summary="") for i in range(3)],
        logic=QuestLogic(
            variables=[LogicVar(id="stuck", type=LogicVarType.BOOL, default=False)],
            branches=[
                Branch(id="b_good", from_stage="s0", to_stage="s1"),
                Branch(
                    id="b_trap", from_stage="s0", to_stage="s2",
                    effects=[Effect(var="stuck", op="set", value=True)],
                ),
                Branch(id="b_win", from_stage="s1", outcome="win"),
                Branch(id="b_exit", from_stage="s2", condition="not stuck", outcome="escaped"),
            ],
        ),
    )


def test_rule_is_registered_in_the_default_registry() -> None:
    assert _RULE in build_default_rule_registry().codes()


def test_soft_lock_is_reported_through_the_audit_runner() -> None:
    result = _run(_bundle_with(_softlock_quest()))
    soft = [i for i in result.issues if i.rule_code == _RULE and "QUEST_SOFT_LOCK" in i.message]
    assert len(soft) == 1
    issue = soft[0]
    assert issue.severity is Severity.ERROR
    assert issue.evidence[0].data["witness"] == ["b_trap"]
    assert issue.evidence[0].data["pattern"] == "AG_EF_ENDING"
    # a sound gate: the soft-lock issue is an OPEN error in the same run
    assert issue in result.open_errors


def _mutex_violating_quest() -> Quest:
    return Quest(
        id="q_mutex", title="t",
        stages=[QuestStage(id="s0", summary=""), QuestStage(id="s1", summary="")],
        logic=QuestLogic(
            variables=[
                LogicVar(id="a", type=LogicVarType.BOOL, default=False),
                LogicVar(id="b", type=LogicVarType.BOOL, default=False),
            ],
            stage_logic=[StageLogic(stage_id="s0", effects_on_complete=[
                Effect(var="a", op="set", value=True), Effect(var="b", op="set", value=True)])],
            branches=[
                Branch(id="adv", from_stage="s0", to_stage="s1"),
                Branch(id="fin", from_stage="s1", outcome="done"),
            ],
        ),
    )


def test_declared_ag_invariant_violation_is_reported() -> None:
    quest = _mutex_violating_quest()
    quest.metadata["temporal_specs"] = [
        {"id": "mutex", "pattern": "AG", "expression": "not (a and b)", "description": "互斥"}
    ]
    issues = [
        i for i in _temporal_issues(_bundle_with(quest)) if "AG_INVARIANT_VIOLATED" in i.message
    ]
    assert len(issues) == 1
    assert issues[0].severity is Severity.ERROR
    assert issues[0].evidence[0].data["witness"] == ["adv"]


def test_declared_invariant_that_holds_emits_nothing() -> None:
    quest = _mutex_violating_quest()
    # Use a genuinely-satisfied invariant: flag 'a' is always eventually set → EF a holds → silent.
    quest.metadata["temporal_specs"] = [{"id": "reach_a", "pattern": "EF", "expression": "a"}]
    issues = _temporal_issues(_bundle_with(quest))
    assert issues == []  # a satisfied spec is silent


def test_vacuous_spec_is_a_warning_not_an_error() -> None:
    quest = Quest(
        id="q_vac", title="t",
        stages=[QuestStage(id="s0", summary=""), QuestStage(id="s1", summary="")],
        logic=QuestLogic(
            variables=[
                LogicVar(id="trigger", type=LogicVarType.BOOL, default=False),
                LogicVar(id="conseq", type=LogicVarType.BOOL, default=False),
            ],
            branches=[
                Branch(id="adv", from_stage="s0", to_stage="s1"),
                Branch(id="fin", from_stage="s1", outcome="done"),
            ],
        ),
    )
    quest.metadata["temporal_specs"] = [
        {
            "id": "impl", "pattern": "AG",
            "expression": "not trigger or conseq", "antecedent": "trigger",
        }
    ]
    issues = [i for i in _temporal_issues(_bundle_with(quest)) if "SPEC_VACUOUS" in i.message]
    assert len(issues) == 1
    assert issues[0].severity is Severity.WARNING  # advisory, never a hard-gate error


def test_malformed_spec_is_a_warning_not_an_error() -> None:
    quest = _mutex_violating_quest()
    quest.metadata["temporal_specs"] = [
        {"id": "bad", "pattern": "AG", "expression": "undeclared_flag and ("}  # unparseable
    ]
    issues = [i for i in _temporal_issues(_bundle_with(quest)) if "SPEC_MALFORMED" in i.message]
    assert len(issues) == 1
    assert issues[0].severity is Severity.WARNING


def test_completable_branches_have_no_temporal_issues() -> None:
    quest = Quest(
        id="q_complete", title="Completable state contract",
        stages=[QuestStage(id=key, summary=key) for key in ("start", "left", "right")],
        logic=QuestLogic(
            variables=[LogicVar(id="selected", type=LogicVarType.BOOL, default=False)],
            branches=[
                Branch(id="choose_left", from_stage="start", to_stage="left",
                       effects=[Effect(var="selected", op="set", value=True)]),
                Branch(id="choose_right", from_stage="start", to_stage="right"),
                Branch(id="left_end", from_stage="left", condition="selected", outcome="left"),
                Branch(
                    id="right_end", from_stage="right", condition="not selected", outcome="right"
                ),
            ],
        ),
    )
    assert _temporal_issues(_bundle_with(quest)) == []


def test_quests_without_logic_are_skipped() -> None:
    quest = Quest(id="q_plain", title="t", stages=[QuestStage(id="s0", summary="")])
    assert _temporal_issues(_bundle_with(quest)) == []


# --- 世界感知解缴械：规则层集成（不只是库层）-----------------------------------------------------


def _world_gated_quest() -> Quest:
    """``s_act`` 与它唯一的出口都需要 ``helped``，而这条任务自己从不写它。单任务视角必须弃权；
    规则层建了世界级交叉引用之后，"全世界无人写它"就足以把弃权收窄成确定的软锁。"""
    return Quest(
        id="q_gate",
        title="q_gate",
        stages=[QuestStage(id="s_talk", summary=""), QuestStage(id="s_act", summary="")],
        logic=QuestLogic(
            variables=[LogicVar(id="helped", type=LogicVarType.BOOL, default=False)],
            stage_logic=[StageLogic(stage_id="s_act", precondition="helped")],
            branches=[
                Branch(id="b_open", from_stage="s_talk", to_stage="s_act"),
                Branch(id="b_done", from_stage="s_act", condition="helped", outcome="done"),
            ],
        ),
    )


def test_world_sealed_soft_lock_is_reported_through_the_audit_rule() -> None:
    """守的是 temporal_rules.check() 里那次世界级交叉引用的传递：把它删掉，库层测试全绿，
    但这条会红——弃权会吞掉这个全世界无人解得开的软锁。"""
    issues = [
        i
        for i in _temporal_issues(_bundle_with(_world_gated_quest()))
        if "QUEST_SOFT_LOCK" in i.message
    ]
    assert len(issues) == 1
    assert issues[0].severity is Severity.ERROR
    assert "helped" in issues[0].message  # 点名那个全世界无人写入的变量


def test_a_writer_anywhere_in_the_world_keeps_the_withhold() -> None:
    """误报边界：另一条任务写了 ``helped``，弃权就仍然是正确判定——规则层不得报软锁。"""
    writer = Quest(
        id="q_writer",
        title="q_writer",
        stages=[QuestStage(id="s0", summary="")],
        logic=QuestLogic(
            variables=[LogicVar(id="helped", type=LogicVarType.BOOL, default=False)],
            stage_logic=[
                StageLogic(
                    stage_id="s0",
                    effects_on_complete=[Effect(var="helped", op="set", value=True)],
                )
            ],
        ),
    )
    bundle = ContentBundle()
    bundle.quests["q_gate"] = _world_gated_quest()
    bundle.quests["q_writer"] = writer

    assert [i for i in _temporal_issues(bundle) if "QUEST_SOFT_LOCK" in i.message] == []
