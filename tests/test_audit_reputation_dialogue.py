"""F4/A7 + A5: faction-reputation reference validation and dialogue-condition variable scope."""

from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.dialogue_rules import DialogueChoiceConditionRule
from owcopilot.audit.rules.logic_rules import QuestLogicRule
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
)


def _faction(fac_id: str) -> Entity:
    return Entity(id=fac_id, name=fac_id, type=EntityType.FACTION)


def _quest_with_rep(target_faction: str) -> Quest:
    return Quest(
        id="q_choice",
        title="A choice with consequences",
        stages=[QuestStage(id="s1", summary="decide"), QuestStage(id="s2", summary="aftermath")],
        logic=QuestLogic(
            branches=[
                Branch(
                    id="b1",
                    from_stage="s1",
                    to_stage="s2",
                    effects=[Effect(var=f"rep:{target_faction}", op="inc", value=10)],
                )
            ]
        ),
    )


def test_reputation_ref_to_real_faction_is_clean() -> None:
    bundle = ContentBundle(
        entities={"fac_iron": _faction("fac_iron")},
        quests={"q_choice": _quest_with_rep("fac_iron")},
    )
    issues = list(QuestLogicRule().check(AuditContext.from_bundle(bundle)))
    assert not any("reputation reference" in i.message for i in issues)


def test_reputation_ref_to_unknown_faction_is_flagged() -> None:
    bundle = ContentBundle(
        entities={"fac_iron": _faction("fac_iron")},
        quests={"q_choice": _quest_with_rep("fac_ghost")},  # not a real faction
    )
    issues = list(QuestLogicRule().check(AuditContext.from_bundle(bundle)))
    rep_issues = [i for i in issues if "reputation reference 'rep:fac_ghost'" in i.message]
    assert len(rep_issues) == 1
    assert rep_issues[0].rule_code == "QUEST_LOGIC"


def test_dialogue_condition_flags_only_undeclared_variable() -> None:
    quest = Quest(
        id="q1",
        title="Q1",
        stages=[QuestStage(id="s1", summary="x")],
        logic=QuestLogic(variables=[LogicVar(id="has_key", type=LogicVarType.BOOL)]),
    )
    tree = DialogueTree(
        id="dt1",
        quest_id="q1",
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                text="...",
                choices=[
                    DialogueChoice(text="declared", condition="has_key"),  # ok
                    DialogueChoice(text="quest state", condition="quest:q1.done"),  # ok
                    DialogueChoice(text="reputation", condition="rep:fac_iron >= 5"),  # ok
                    DialogueChoice(text="bogus", condition="mystery_flag"),  # undefined
                ],
            )
        },
    )
    bundle = ContentBundle(
        entities={"fac_iron": _faction("fac_iron")},
        quests={"q1": quest},
        dialogue_trees={"dt1": tree},
    )
    issues = list(DialogueChoiceConditionRule().check(AuditContext.from_bundle(bundle)))
    assert len(issues) == 1
    assert issues[0].rule_code == "DIALOGUE_CONDITION_UNDEFINED_VAR"
    assert "mystery_flag" in issues[0].message


def test_dialogue_condition_skips_trees_without_quest_logic() -> None:
    # A standalone tree (no quest_id) has no variable scope to validate — no false positives.
    node = DialogueNode(id="n1", choices=[DialogueChoice(text="x", condition="anything")])
    tree = DialogueTree(id="dt2", root_node="n1", nodes={"n1": node})
    bundle = ContentBundle(dialogue_trees={"dt2": tree})
    issues = list(DialogueChoiceConditionRule().check(AuditContext.from_bundle(bundle)))
    assert issues == []
