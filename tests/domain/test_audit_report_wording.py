# 可执行报告改造·验收：对话/逻辑/时序/世界级 flag 十条规则的中文消息 + 实例化建议动作。
"""验收 ``tests/domain/test_audit_report_wording.py`` 交给本轮的九条规则
（dialogue_rules.py 七条、logic_rules.py 一条、temporal_rules.py 一条），外加后续并入
同一道文案门的 flag_rules.py 一条
（``WORLD_FLAG_CONSISTENCY``，四个子判定各自成条）：

* 消息是策划看得懂的中文，不含蛇形命名/类名这类代码标识符；
* 建议动作里出现这一条实例specific 的字段名或值（不是规则的泛泛描述）；
* 有自动修复器的规则本组一条都没有——七个修复器（``patches/fixers.py``）对应的 rule_code 全部
  属于 reference_rules.py / graph_rules.py / pipeline_rules.py，不在本组内。

测试只组装输入、断言产出的 ``Issue.message``/``Issue.suggested_action``，不重写任何规则的判定逻辑。
"""

from __future__ import annotations

import re

from owcopilot.audit.context import AuditContext
from owcopilot.audit.models import Issue
from owcopilot.audit.rules.dialogue_rules import (
    DialogueChoiceConditionRule,
    DialogueSelfReportInconsistencyRule,
    DialogueTreeBrokenLinkRule,
    DialogueTreeMissingTextKeyRule,
    DialogueTreeUnknownSpeakerRule,
    DialogueTreeUnreachableNodeRule,
    DialogueUngatedSiblingChoiceRule,
)
from owcopilot.audit.rules.flag_rules import WorldFlagConsistencyRule
from owcopilot.audit.rules.logic_rules import QuestLogicRule
from owcopilot.audit.rules.temporal_rules import FlagTemporalInvariantRule
from owcopilot.content.models import (
    Branch,
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
    Entity,
    EntityType,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)

_CJK = re.compile(r"[一-鿿]")

# 这几个规则文件本可能直接把 Python/schema 层的字段名（蛇形命名）漏进策划文案；对象 id 与节点 id
# 允许出现，但这些内部字段名/类名不许出现在 message/suggested_action 里。
_BANNED_IDENTIFIERS = (
    "next_node", "text_key", "root_node", "speaker_id", "quest_id", "stage_id",
    "stage_logic", "effects_on_complete", "from_stage", "to_stage", "dialogue_refs",
    "localization_keys", "required_entities", "giver_npc", "controlling_faction",
    "rule_code", "target_ref", "self.",
    "DialogueTree", "DialogueNode", "DialogueChoice", "QuestLogicRule",
    "FlagTemporalInvariantRule", "audit_quest_logic",
)


def _assert_planner_friendly(issue: Issue) -> None:
    """消息是中文、建议非空、两者都不含代码标识符——三条机械检查里能在单元测试层面验的那两条半。"""
    assert _CJK.search(issue.message), f"消息不是中文：{issue.message!r}"
    assert issue.suggested_action.strip(), "建议动作不能是空的"
    for text in (issue.message, issue.suggested_action):
        for token in _BANNED_IDENTIFIERS:
            assert token not in text, f"文案里混进了代码标识符 {token!r}：{text!r}"


def _bundle(**collections) -> ContentBundle:
    return ContentBundle(**collections)


def _issues(rule, bundle: ContentBundle) -> list[Issue]:
    return list(rule.check(AuditContext.from_bundle(bundle)))


# --------------------------------------------------------------------------------------------------
# 1) DIALOGUE_TREE_BROKEN_LINK
# --------------------------------------------------------------------------------------------------
def test_broken_link_message_and_action_name_the_missing_node() -> None:
    tree = DialogueTree(
        id="dt_gift",
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                choices=[DialogueChoice(text="送礼物", next_node="n_ghost")],
            )
        },
    )
    issues = _issues(DialogueTreeBrokenLinkRule(), _bundle(dialogue_trees={"dt_gift": tree}))
    assert len(issues) == 1
    issue = issues[0]
    _assert_planner_friendly(issue)
    assert "第 n1 个节点的第 1 个选项" in issue.message
    assert "n_ghost" in issue.message
    assert "n_ghost" in issue.suggested_action  # 建议里点名了具体缺失的节点 id
    assert issue.node_ref == "n1"


# --------------------------------------------------------------------------------------------------
# 2) DIALOGUE_TREE_UNKNOWN_SPEAKER
# --------------------------------------------------------------------------------------------------
def test_unknown_speaker_action_names_the_missing_speaker_id() -> None:
    tree = DialogueTree(
        id="dt_meet",
        root_node="n1",
        nodes={"n1": DialogueNode(id="n1", speaker_id="npc_ghost", text="你好")},
    )
    issues = _issues(DialogueTreeUnknownSpeakerRule(), _bundle(dialogue_trees={"dt_meet": tree}))
    assert len(issues) == 1
    issue = issues[0]
    _assert_planner_friendly(issue)
    assert "npc_ghost" in issue.message
    assert "'npc_ghost'" in issue.suggested_action


# --------------------------------------------------------------------------------------------------
# 3) DIALOGUE_CONDITION_UNDEFINED_VAR (+ 内嵌的 DIALOGUE_CONDITION_SYNTAX_ERROR)
# --------------------------------------------------------------------------------------------------
def _quest_with_var(var_id: str = "has_key") -> Quest:
    return Quest(
        id="q1",
        title="钥匙",
        stages=[QuestStage(id="s1", summary="x")],
        logic=QuestLogic(variables=[LogicVar(id=var_id, type=LogicVarType.BOOL)]),
    )


def test_condition_undeclared_var_action_names_quest_and_variable() -> None:
    quest = _quest_with_var()
    tree = DialogueTree(
        id="dt1",
        quest_id="q1",
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                choices=[DialogueChoice(text="乱猜", condition="mystery_flag")],
            )
        },
    )
    bundle = _bundle(quests={"q1": quest}, dialogue_trees={"dt1": tree})
    issues = _issues(DialogueChoiceConditionRule(), bundle)
    assert len(issues) == 1
    issue = issues[0]
    _assert_planner_friendly(issue)
    assert "mystery_flag" in issue.message and "q1" in issue.message
    assert "mystery_flag" in issue.suggested_action and "q1" in issue.suggested_action


def test_condition_syntax_error_action_points_at_the_exact_choice() -> None:
    quest = _quest_with_var()
    tree = DialogueTree(
        id="dt1",
        quest_id="q1",
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                choices=[DialogueChoice(text="坏条件", condition="has_key ==")],
            )
        },
    )
    bundle = _bundle(quests={"q1": quest}, dialogue_trees={"dt1": tree})
    issues = _issues(DialogueChoiceConditionRule(), bundle)
    assert len(issues) == 1
    issue = issues[0]
    _assert_planner_friendly(issue)
    assert "第 n1 个节点的第 1 个选项" in issue.message
    assert "第 n1 个节点的第 1 个选项" in issue.suggested_action


def test_effect_undeclared_var_message_says_effect_not_condition() -> None:
    quest = _quest_with_var()
    tree = DialogueTree(
        id="dt1",
        quest_id="q1",
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                choices=[
                    DialogueChoice(
                        text="做点什么",
                        effects=[Effect(var="ghost_var", op="set", value=True)],
                    )
                ],
            )
        },
    )
    bundle = _bundle(quests={"q1": quest}, dialogue_trees={"dt1": tree})
    issues = _issues(DialogueChoiceConditionRule(), bundle)
    assert len(issues) == 1
    issue = issues[0]
    _assert_planner_friendly(issue)
    assert "ghost_var" in issue.message and "选中后会修改变量" in issue.message


# --------------------------------------------------------------------------------------------------
# 4) DIALOGUE_TREE_MISSING_TEXT_KEY
# --------------------------------------------------------------------------------------------------
def test_missing_text_key_action_gives_the_conventional_key() -> None:
    tree = DialogueTree(
        id="dt1",
        root_node="n1",
        nodes={"n1": DialogueNode(id="n1", text="你好，旅人。")},
    )
    issues = _issues(DialogueTreeMissingTextKeyRule(), _bundle(dialogue_trees={"dt1": tree}))
    assert len(issues) == 1
    issue = issues[0]
    _assert_planner_friendly(issue)
    assert "本地化键" in issue.message
    assert "'dlg.dt1.n1'" in issue.suggested_action


def test_missing_text_key_on_a_choice_uses_the_choice_index_convention() -> None:
    tree = DialogueTree(
        id="dt1",
        root_node="n1",
        nodes={"n1": DialogueNode(id="n1", choices=[DialogueChoice(text="嗯。")])},
    )
    issues = _issues(DialogueTreeMissingTextKeyRule(), _bundle(dialogue_trees={"dt1": tree}))
    assert len(issues) == 1
    assert "'dlg.dt1.n1.c0'" in issues[0].suggested_action


# --------------------------------------------------------------------------------------------------
# 5) DIALOGUE_TREE_UNREACHABLE_NODE
# --------------------------------------------------------------------------------------------------
def test_unreachable_node_action_names_the_exact_node() -> None:
    tree = DialogueTree(
        id="dt1",
        root_node="n1",
        nodes={"n1": DialogueNode(id="n1"), "n_orphan": DialogueNode(id="n_orphan")},
    )
    issues = _issues(DialogueTreeUnreachableNodeRule(), _bundle(dialogue_trees={"dt1": tree}))
    assert len(issues) == 1
    issue = issues[0]
    _assert_planner_friendly(issue)
    assert "n_orphan" in issue.message
    assert "n_orphan" in issue.suggested_action


# --------------------------------------------------------------------------------------------------
# 6) DIALOGUE_SELF_REPORT_INCONSISTENCY
# --------------------------------------------------------------------------------------------------
def test_self_report_inconsistency_action_gives_a_concrete_condition_literal() -> None:
    tree = DialogueTree(
        id="dt1",
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                choices=[
                    DialogueChoice(
                        text="我读过书",
                        next_node="n2",
                        effects=[Effect(var="said_read_books", op="set", value=True)],
                    )
                ],
            ),
            "n2": DialogueNode(
                id="n2",
                choices=[
                    DialogueChoice(
                        text="我从没读过书",
                        effects=[Effect(var="said_read_books", op="set", value=False)],
                    )
                ],
            ),
        },
    )
    issues = _issues(
        DialogueSelfReportInconsistencyRule(), _bundle(dialogue_trees={"dt1": tree})
    )
    assert len(issues) == 1
    issue = issues[0]
    _assert_planner_friendly(issue)
    assert "said_read_books" in issue.message
    assert '"said_read_books == false"' in issue.suggested_action


# --------------------------------------------------------------------------------------------------
# 7) DIALOGUE_UNGATED_SIBLING_CHOICE
# --------------------------------------------------------------------------------------------------
def test_ungated_sibling_action_quotes_the_existing_gated_condition() -> None:
    tree = DialogueTree(
        id="dt1",
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                choices=[
                    DialogueChoice(
                        text="说读过书",
                        next_node="n2",
                        effects=[Effect(var="said_read_books", op="set", value=True)],
                    )
                ],
            ),
            "n2": DialogueNode(
                id="n2",
                choices=[
                    DialogueChoice(text="也曾读过几年书", condition="said_read_books == true"),
                    DialogueChoice(text="不曾读书"),
                ],
            ),
        },
    )
    issues = _issues(
        DialogueUngatedSiblingChoiceRule(), _bundle(dialogue_trees={"dt1": tree})
    )
    assert len(issues) == 1
    issue = issues[0]
    _assert_planner_friendly(issue)
    assert "said_read_books" in issue.message
    assert "said_read_books == true" in issue.suggested_action  # 引用了已有条件的原文


# --------------------------------------------------------------------------------------------------
# 8) QUEST_LOGIC（含内嵌子代码：未定义变量/类型错误/不可达阶段/死局/悬空引用/声望悬空）
# --------------------------------------------------------------------------------------------------
def _linear_quest(logic: QuestLogic, n: int = 3) -> Quest:
    return Quest(
        id="q_logic",
        title="t",
        stages=[QuestStage(id=f"s{i}", summary="") for i in range(n)],
        logic=logic,
    )


def test_undefined_var_action_names_quest_and_variable() -> None:
    logic = QuestLogic(stage_logic=[StageLogic(stage_id="s1", precondition="ghost_flag")])
    quest = _linear_quest(logic)
    issues = _issues(QuestLogicRule(), _bundle(quests={"q_logic": quest}))
    hit = [i for i in issues if "LOGIC_UNDEFINED_VAR" in i.message]
    assert len(hit) == 1
    issue = hit[0]
    _assert_planner_friendly(issue)
    assert "ghost_flag" in issue.message and "q_logic" in issue.message
    assert "ghost_flag" in issue.suggested_action and "q_logic" in issue.suggested_action


def test_type_mismatch_action_is_chinese_and_locates_the_stage() -> None:
    quest = _linear_quest(
        QuestLogic(
            variables=[LogicVar(id="flag", type=LogicVarType.BOOL)],
            stage_logic=[StageLogic(stage_id="s1", precondition="flag > 3")],
        )
    )
    issues = _issues(QuestLogicRule(), _bundle(quests={"q_logic": quest}))
    hit = [i for i in issues if "LOGIC_TYPE_MISMATCH" in i.message]
    assert len(hit) == 1
    issue = hit[0]
    _assert_planner_friendly(issue)
    assert "s1" in issue.message
    assert "数字" in issue.message  # 'flag > 3' 是整数比较，报出的类型名被翻成了中文


def test_unreachable_stage_and_deadlock_actions_name_concrete_stage_ids() -> None:
    quest = _linear_quest(
        QuestLogic(
            branches=[
                Branch(id="b1", from_stage="s0", to_stage="s1"),
                Branch(id="b2", from_stage="s1", to_stage="s0"),
            ]
        )
    )
    issues = _issues(QuestLogicRule(), _bundle(quests={"q_logic": quest}))
    unreachable = [i for i in issues if "LOGIC_UNREACHABLE_STAGE" in i.message]
    deadlock = [i for i in issues if "LOGIC_DEADLOCK" in i.message]
    assert len(unreachable) == 1 and len(deadlock) == 1
    _assert_planner_friendly(unreachable[0])
    _assert_planner_friendly(deadlock[0])
    assert "s2" in unreachable[0].message and "s2" in unreachable[0].suggested_action
    assert "s0" in deadlock[0].suggested_action  # 首阶段 id 具体点出


def test_dangling_stage_ref_action_names_the_missing_stage() -> None:
    quest = _linear_quest(QuestLogic(stage_logic=[StageLogic(stage_id="ghost_stage")]), n=1)
    issues = _issues(QuestLogicRule(), _bundle(quests={"q_logic": quest}))
    hit = [
        i
        for i in issues
        if "LOGIC_DANGLING_STATE_REF" in i.message and "ghost_stage" in i.message
    ]
    assert len(hit) == 1
    issue = hit[0]
    _assert_planner_friendly(issue)
    assert "ghost_stage" in issue.suggested_action


def test_reputation_dangling_action_names_quest_and_faction() -> None:
    quest = _linear_quest(
        QuestLogic(
            branches=[
                Branch(
                    id="b1", from_stage="s0", to_stage="s1",
                    effects=[Effect(var="rep:fac_ghost", op="inc", value=5)],
                )
            ]
        )
    )
    fac_iron = Entity(id="fac_iron", name="铁盟", type=EntityType.FACTION)
    bundle = _bundle(quests={"q_logic": quest}, entities={"fac_iron": fac_iron})
    issues = _issues(QuestLogicRule(), bundle)
    hit = [i for i in issues if "阵营声望" in i.message]
    assert len(hit) == 1
    issue = hit[0]
    _assert_planner_friendly(issue)
    assert "fac_ghost" in issue.message and "q_logic" in issue.message
    assert "fac_ghost" in issue.suggested_action


# --------------------------------------------------------------------------------------------------
# 9) FLAG_TEMPORAL_INVARIANT
# --------------------------------------------------------------------------------------------------
def test_soft_lock_action_names_the_stage_and_witness_path() -> None:
    quest = Quest(
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
    bundle = _bundle(quests={"q_softlock": quest})
    issues = _issues(FlagTemporalInvariantRule(), bundle)
    hit = [i for i in issues if "QUEST_SOFT_LOCK" in i.message]
    assert len(hit) == 1
    issue = hit[0]
    _assert_planner_friendly(issue)
    assert "s2" in issue.suggested_action and "b_trap" in issue.suggested_action


# --------------------------------------------------------------------------------------------------
# 10) WORLD_FLAG_CONSISTENCY —— 四个子判定各自的文案
# --------------------------------------------------------------------------------------------------
def _flag_quest(quest_id: str, logic: QuestLogic, order: int | None = None) -> Quest:
    return Quest(
        id=quest_id,
        title=quest_id,
        timeline_order=order,
        stages=[QuestStage(id="s0", summary="")],
        logic=logic,
    )


def _writes_flag(flag: str) -> QuestLogic:
    return QuestLogic(
        variables=[LogicVar(id=flag, type=LogicVarType.BOOL, default=False)],
        stage_logic=[
            StageLogic(stage_id="s0", effects_on_complete=[Effect(var=flag, op="set", value=True)])
        ],
    )


def _reads_flag(flag: str) -> QuestLogic:
    return QuestLogic(
        variables=[LogicVar(id=flag, type=LogicVarType.BOOL, default=False)],
        branches=[Branch(id="b0", from_stage="s0", condition=f"{flag} == true", outcome="done")],
    )


def _flag_issue(bundle: ContentBundle, subcode: str) -> Issue:
    hit = [i for i in _issues(WorldFlagConsistencyRule(), bundle) if subcode in i.message]
    assert len(hit) == 1, f"expected exactly one {subcode}, got {[i.message for i in hit]}"
    return hit[0]


def test_flag_declaration_conflict_action_names_the_variable() -> None:
    reader = _flag_quest("q_b", _reads_flag("shared"))
    reader.logic.variables[0] = LogicVar(id="shared", type=LogicVarType.INT, default=3)  # type: ignore[union-attr]
    bundle = _bundle(quests={"q_a": _flag_quest("q_a", _writes_flag("shared")), "q_b": reader})

    issue = _flag_issue(bundle, "FLAG_DECL_CONFLICT")

    _assert_planner_friendly(issue)
    assert "shared" in issue.suggested_action
    assert "q_a" in issue.message and "q_b" in issue.message


def test_flag_never_written_action_names_the_variable_and_the_way_out() -> None:
    bundle = _bundle(quests={"q_b": _flag_quest("q_b", _reads_flag("orphan"))})

    issue = _flag_issue(bundle, "FLAG_NEVER_WRITTEN")

    _assert_planner_friendly(issue)
    assert "orphan" in issue.suggested_action


def test_flag_never_read_action_names_the_variable_and_allows_the_engine_case() -> None:
    bundle = _bundle(quests={"q_a": _flag_quest("q_a", _writes_flag("counter"))})

    issue = _flag_issue(bundle, "FLAG_NEVER_READ")

    _assert_planner_friendly(issue)
    assert "counter" in issue.suggested_action
    assert "引擎" in issue.suggested_action  # 留给引擎侧读的情形要给出台阶，避免逼人改内容


def test_phase_reference_too_early_message_gives_both_timeline_positions() -> None:
    bundle = _bundle(
        quests={
            "q_early": _flag_quest("q_early", _reads_flag("later_flag"), order=1),
            "q_late": _flag_quest("q_late", _writes_flag("later_flag"), order=5),
        }
    )

    issue = _flag_issue(bundle, "PHASE_REFERENCE_TOO_EARLY")

    _assert_planner_friendly(issue)
    assert "later_flag" in issue.suggested_action
    assert "第 5 位" in issue.message and "第 1 位" in issue.message
