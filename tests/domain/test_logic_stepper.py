"""Explicit actions share production semantics with batch playtests; no simulated evaluator."""

from __future__ import annotations

import json

import pytest

from owcopilot.assist.dialogue_trees import parse_dialogue_tree
from owcopilot.content.models import (
    Branch,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
    LogicVar,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.logic import simulate_quest, walk_dialogue_tree
from owcopilot.logic.stepper import (
    PreviewSession,
    advance_preview,
    preview_view,
    start_preview,
)


def test_stage_projection_waits_and_one_action_applies_effects_once() -> None:
    quest = Quest(
        id="gate",
        title="守门人",
        stages=[QuestStage(id="pay", summary="支付过路费")],
        logic=QuestLogic(
            variables=[LogicVar(id="gold", type="int", default=2)],
            stage_logic=[
                StageLogic(
                    stage_id="pay", effects_on_complete=[Effect(var="gold", op="dec", value=1)]
                )
            ],
            branches=[
                Branch(id="enter", from_stage="pay", condition="gold == 1", outcome="入城"),
                Branch(id="rich", from_stage="pay", condition="gold == 2", outcome="错误入口"),
            ],
        ),
    )
    session = start_preview(quest=quest)
    assert session.status == "waiting_choice"
    assert session.values["gold"] == 2
    first_view = preview_view(session)
    assert first_view == preview_view(session)
    assert first_view["choices"] == [{"id": "enter", "label": "入城", "target": None}]
    with pytest.raises(ValueError, match="选择"):
        advance_preview(session, action_id="unselected")
    with pytest.raises(ValueError, match="可用选项"):
        advance_preview(session, action_id="bad", choice_id="rich")

    done = advance_preview(session, action_id="pay-once", choice_id="enter", expected_seq=0)
    assert done.status == "completed" and done.outcome == "入城"
    assert done.values["gold"] == 1 and session.values["gold"] == 2
    replayed = advance_preview(done, action_id="pay-once", choice_id="enter", expected_seq=0)
    assert replayed.model_dump() == done.model_dump()
    assert simulate_quest(quest, choices=["enter"]).final_state == done.values


def test_dialogue_and_quest_share_initial_state_and_survive_serialization() -> None:
    quest = Quest(
        id="gate",
        title="入城",
        stages=[QuestStage(id="enter", summary="出示通行证")],
        logic=QuestLogic(
            variables=[LogicVar(id="permission", default=False)], precondition="permission"
        ),
    )
    tree = DialogueTree(
        id="guard",
        quest_id="gate",
        root_node="ask",
        nodes={
            "ask": DialogueNode(
                id="ask",
                text="出示信件",
                choices=[
                    DialogueChoice(
                        text="递交",
                        condition="letter",
                        effects=[Effect(var="permission", value=True)],
                    )
                ],
            )
        },
    )
    session = start_preview(
        quest=quest, dialogue_tree=tree, initial_state={"letter": True, "permission": False}
    )
    session = PreviewSession.model_validate_json(session.model_dump_json())
    assert preview_view(session)["text"] == "出示信件"
    after_dialogue = advance_preview(session, action_id="give", choice_id="ask:0")
    assert after_dialogue.phase == "quest" and after_dialogue.values["permission"] is True
    done = advance_preview(after_dialogue, action_id="enter", expected_seq=1)
    assert done.status == "completed" and done.action_seq == 2
    assert [row.phase for row in done.history] == ["dialogue", "quest"]
    assert (
        walk_dialogue_tree(tree, initial_state={"letter": True}).final_state["permission"] is True
    )
    # A delayed replay cannot rewind the completed quest or apply the dialogue effects again.
    assert (
        advance_preview(done, action_id="give", choice_id="ask:0").model_dump() == done.model_dump()
    )


def test_preview_rejects_stale_actions_and_reports_missing_logic_and_cycles() -> None:
    tree = DialogueTree(
        id="loop", root_node="a", nodes={"a": DialogueNode(id="a", text="继续", next_node="a")}
    )
    session = start_preview(dialogue_tree=tree)
    with pytest.raises(ValueError, match="过期"):
        advance_preview(session, action_id="later", expected_seq=1)
    assert advance_preview(session, action_id="next").status == "cycle"
    assert start_preview(quest=Quest(id="prose", title="正文")).status == "unsupported"
    with pytest.raises(ValueError, match="不一致"):
        start_preview(
            quest=Quest(id="q", title="Q"),
            dialogue_tree=tree.model_copy(update={"quest_id": "other"}),
        )


def test_false_choices_fall_back_linearly_and_step_bound_is_honest() -> None:
    tree = DialogueTree(
        id="fallback",
        root_node="a",
        nodes={
            "a": DialogueNode(
                id="a",
                next_node="b",
                choices=[
                    DialogueChoice(
                        text="隐藏", condition="false", effects=[Effect(var="bad", value=True)]
                    )
                ],
            ),
            "b": DialogueNode(id="b", text="结束"),
        },
    )
    session = start_preview(dialogue_tree=tree, max_steps=1)
    assert preview_view(session)["choices"] == []
    advanced = advance_preview(session, action_id="continue")
    assert advanced.status == "truncated" and "bad" not in advanced.values
    assert advanced.history[0].cursor == "a"


def test_generated_dialogue_keeps_choice_mechanisms_and_localization_keys() -> None:
    tree = parse_dialogue_tree(
        json.dumps(
            {
                "title": "约定",
                "root": "ask",
                "nodes": [
                    {
                        "id": "ask",
                        "speaker": "npc_guard",
                        "text": "愿意吗？",
                        "text_key": "d.ask",
                        "choices": [
                            {
                                "text": "愿意",
                                "next": None,
                                "condition": "allowed",
                                "text_key": "d.yes",
                                "effects": [{"var": "trusted", "op": "set", "value": True}],
                            }
                        ],
                    }
                ],
            }
        ),
        brief="约定",
        participant_ids=["npc_guard"],
        quest_id="gate",
        existing_ids=set(),
        max_nodes=10,
    )
    choice = tree.nodes["ask"].choices[0]
    assert choice.condition == "allowed" and choice.text_key == "d.yes"
    assert tree.nodes["ask"].text_key == "d.ask"
    blocked = start_preview(dialogue_tree=tree)
    assert advance_preview(blocked, action_id="no").status == "deadlock"
    session = start_preview(dialogue_tree=tree, initial_state={"allowed": True})
    done = advance_preview(session, action_id="yes", choice_id="ask:0")
    assert done.status == "completed" and done.values["trusted"] is True
