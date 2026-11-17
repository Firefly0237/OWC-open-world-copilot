"""Player presentation uses the real stepper; no model or human evaluation is simulated."""

from copy import deepcopy

import pytest

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
from owcopilot.logic.stepper import (
    PreviewSession,
    advance_preview,
    preview_view,
    start_preview,
)


def compiled_case():
    """Controlled bindings in the production compiler format, not a generated work."""
    controls = [
        {
            "stage_id": "meeting",
            "decision_id": "approach",
            "option_index": index,
            "option_id": value,
            "selector_variable": "_owc_sel",
            "branch_id": value,
        }
        for index, value in enumerate(("private", "public"))
    ]
    quest = Quest(
        id="q",
        title="信件",
        stages=[QuestStage(id="meeting", summary="设计稿：全部分支与数值")],
        logic=QuestLogic(
            variables=[
                LogicVar(id="trust", name="信任", type="int", default=0),
                LogicVar(
                    id="_owc_sel",
                    type="enum",
                    default="unset",
                    enum_values=["unset", "private", "public"],
                ),
            ],
            stage_logic=[
                StageLogic(
                    stage_id="meeting", effects_on_complete=[Effect(var="trust", op="inc", value=1)]
                )
            ],
            branches=[
                Branch(
                    id="private",
                    from_stage="meeting",
                    condition="_owc_sel == 'private'",
                    outcome="技术说明：trust增加2，仅结算一次",
                    effects=[Effect(var="trust", op="inc", value=2)],
                ),
                Branch(
                    id="public",
                    from_stage="meeting",
                    condition="_owc_sel == 'public'",
                    outcome="技术说明：trust减少2",
                    effects=[Effect(var="trust", op="dec", value=2)],
                ),
            ],
        ),
        metadata={
            "mechanic_ir_v2": {
                "compiler_version": "mechanic-ir-v2.1",
                "control_bindings": controls,
            }
        },
    )
    tree = DialogueTree(
        id="t",
        quest_id="q",
        root_node="ask",
        nodes={
            "ask": DialogueNode(
                id="ask",
                text="你打算怎样处理这封信？",
                choices=[
                    DialogueChoice(
                        text="私下追问",
                        next_node="reply",
                        effects=[Effect(var="_owc_sel", value="private")],
                    ),
                    DialogueChoice(
                        text="当众质问",
                        next_node="reply",
                        effects=[Effect(var="_owc_sel", value="public")],
                    ),
                ],
            ),
            "reply": DialogueNode(id="reply", text="她把信放回口袋。"),
        },
        metadata={
            "mechanic_ir_v2": {
                "compiler_version": "mechanic-ir-v2.1",
                "mechanic_manifest_hash": "fixture",
            },
            "player_presentation_v1": {
                "schema_version": 1,
                "quest_id": "q",
                "bindings": [
                    {
                        "node_id": "ask",
                        "choice_index": index,
                        "stage_id": "meeting",
                        "branch_id": value,
                        "selector_variable": "_owc_sel",
                        "option_value": value,
                    }
                    for index, value in enumerate(("private", "public"))
                ],
                "automatic_branch_ids": [],
                "endings": {"private": "门外的脚步声渐渐远去。", "public": "众人停下了交谈。"},
            },
        },
    )
    return quest, tree


def choose_and_finish(quest, tree, **kwargs):
    session = start_preview(quest=quest, dialogue_tree=tree, presentation="player", **kwargs)
    session = advance_preview(session, action_id="choose", choice_id="ask:0")
    session = PreviewSession.model_validate_json(session.model_dump_json())
    return advance_preview(session, action_id="finish", expected_seq=1)


def test_player_keeps_content_choice_and_execution_separate_and_idempotent():
    quest, tree = compiled_case()
    session = start_preview(quest=quest, dialogue_tree=tree, presentation="player")
    original = session.model_dump()
    assert preview_view(session) == preview_view(session)
    assert session.model_dump() == original and session.values["trust"] == 0
    assert preview_view(session)["state_names"]["trust"] == "信任"
    assert preview_view(session)["state_changes"] == []
    session = advance_preview(session, action_id="choose", choice_id="ask:0")
    assert session.values["trust"] == 0 and preview_view(session)["text"] == "她把信放回口袋。"
    assert preview_view(session)["state_changes"] == []  # Internal selectors are not game stats.
    done = advance_preview(session, action_id="finish", expected_seq=1)
    assert done.status == "completed" and done.values["trust"] == 3
    assert done.action_seq == 2
    assert [step.phase for step in done.history] == ["dialogue", "dialogue", "quest"]
    assert done.history[-1].choice_id == "private"
    assert done.history[-1].effects == ["trust inc 1", "trust inc 2"]
    view = preview_view(done)
    assert view["text"] == view["outcome"] == "门外的脚步声渐渐远去。"
    assert view["choices"] == [] and not view["can_advance"]
    assert view["state_changes"] == [{"id": "trust", "name": "信任", "before": 0, "after": 3}]
    assert "技术说明" in done.outcome  # Execution evidence is preserved, not rewritten.
    saved = PreviewSession.model_validate_json(done.model_dump_json())
    assert (
        advance_preview(saved, action_id="finish", expected_seq=0).model_dump() == done.model_dump()
    )
    assert (
        advance_preview(saved, action_id="choose", choice_id="ask:0").model_dump()
        == done.model_dump()
    )
    with pytest.raises(ValueError, match="不同选择"):
        advance_preview(saved, action_id="choose", choice_id="ask:1")
    old = done.model_dump(exclude={"last_state_changes"})
    assert preview_view(PreviewSession.model_validate(old))["state_changes"] == []


def test_default_logic_and_uncompiled_legacy_player_keep_explicit_stage_semantics():
    quest, tree = compiled_case()
    for presentation in ("logic", "player"):
        if presentation == "player":
            quest.metadata, tree.metadata = {}, {}
        session = start_preview(quest=quest, dialogue_tree=tree, presentation=presentation)
        session = advance_preview(session, action_id="choose", choice_id="ask:0")
        session = advance_preview(session, action_id="finish")
        assert session.phase == "quest" and session.status == "waiting_choice"
        assert session.values["trust"] == 0
        assert preview_view(session)["text"] == "设计稿：全部分支与数值"
        done = advance_preview(session, action_id="settle", choice_id="private")
        assert done.values["trust"] == 3 and done.action_seq == 3


def test_legacy_compiled_binding_settles_without_inventing_an_ending():
    quest, tree = compiled_case()
    del tree.metadata["player_presentation_v1"]
    done = choose_and_finish(quest, tree)
    assert done.status == "completed" and done.values["trust"] == 3
    assert preview_view(done)["text"] == preview_view(done)["outcome"] == ""
    assert "技术说明" in done.outcome


def test_compiled_quest_without_bound_dialogue_cannot_claim_a_selected_player_route():
    quest, _ = compiled_case()
    session = start_preview(quest=quest, presentation="player")
    assert session.status == "blocked" and session.values["trust"] == 0
    assert session.history == []
    assert preview_view(session)["choices"] == [] and preview_view(session)["text"] == ""


def test_state_changes_describe_the_last_action_not_the_whole_preview():
    quest = Quest(
        id="q",
        title="旅途",
        stages=[
            QuestStage(id="one", summary="第一日"),
            QuestStage(id="two", summary="第二日"),
        ],
        logic=QuestLogic(
            variables=[LogicVar(id="trust", name="信任", type="int", default=0)],
            stage_logic=[
                StageLogic(
                    stage_id="one", effects_on_complete=[Effect(var="trust", op="inc", value=1)]
                ),
                StageLogic(
                    stage_id="two", effects_on_complete=[Effect(var="trust", op="inc", value=2)]
                ),
            ],
        ),
    )
    session = advance_preview(start_preview(quest=quest), action_id="one")
    assert preview_view(session)["state_changes"][0]["before"] == 0
    done = advance_preview(session, action_id="two")
    assert preview_view(done)["state_changes"] == [
        {"id": "trust", "name": "信任", "before": 1, "after": 3},
    ]


@pytest.mark.parametrize(
    "fault",
    [
        "missing_binding",
        "widened_condition",
        "unknown_unselected_guard",
        "unknown_stage_condition",
        "false_stage_condition",
        "duplicate_branch",
        "forged_selector",
    ],
)
def test_bad_or_unknown_bindings_never_apply_quest_effects_or_leak_design(fault):
    quest, tree = compiled_case()
    if fault == "missing_binding":
        tree.metadata["player_presentation_v1"]["bindings"].pop()
    elif fault == "widened_condition":
        quest.logic.branches[1].condition = "true"  # Would make two branches legal.
    elif fault == "unknown_unselected_guard":
        quest.logic.branches[1].condition += " and missing_flag"
    elif fault == "unknown_stage_condition":
        quest.logic.stage_logic[0].precondition = "missing_flag"
    elif fault == "false_stage_condition":
        quest.logic.stage_logic[0].precondition = "false"
    elif fault == "duplicate_branch":
        quest.logic.branches.append(deepcopy(quest.logic.branches[0]))
    if fault == "forged_selector":
        tree.root_node = "reply"  # A preset selector is not evidence of a player choice.
        session = start_preview(
            quest=quest,
            dialogue_tree=tree,
            presentation="player",
            initial_state={"_owc_sel": "private"},
        )
        done = advance_preview(session, action_id="finish")
    else:
        done = choose_and_finish(quest, tree)
    assert done.status == "blocked" and done.values["trust"] == 0
    assert all(step.phase == "dialogue" for step in done.history)
    view = preview_view(done)
    assert view["text"] == "" and view["choices"] == [] and view["message"]


def test_internal_settlement_still_respects_history_limit_and_cycles():
    quest, tree = compiled_case()
    done = choose_and_finish(quest, tree, max_steps=2)
    assert done.status == "truncated" and done.values["trust"] == 0
    quest.logic.branches[0].to_stage = "meeting"
    cycle = choose_and_finish(quest, tree)
    assert cycle.status == "cycle" and cycle.values["trust"] == 3
    assert sum(step.phase == "quest" for step in cycle.history) == 1


def test_multistage_split_merge_settles_only_selected_route_after_restore():
    quest, tree = compiled_case()
    quest.stages.extend(
        QuestStage(id=value, summary=f"设计稿：{value}")
        for value in ("records", "witness", "hearing")
    )
    quest.logic.branches[0].to_stage, quest.logic.branches[1].to_stage = "records", "witness"
    for stage, target, delta in (
        ("records", "hearing", 5),
        ("witness", "hearing", 100),
        ("hearing", "", 7),
    ):
        quest.logic.branches.append(
            Branch(
                id=f"auto_{stage}",
                from_stage=stage,
                to_stage=target,
                effects=[Effect(var="trust", op="inc", value=delta)],
                outcome=f"设计稿：{stage}结算",
            )
        )
    spec = tree.metadata["player_presentation_v1"]
    spec["automatic_branch_ids"] = ["auto_records", "auto_witness", "auto_hearing"]
    spec["endings"] = {"auto_hearing": "审讯室的门在你身后合上。"}
    tree.nodes["reply"].next_node = "hearing_text"
    tree.nodes["hearing_text"] = DialogueNode(id="hearing_text", text="书记官抬起了头。")
    session = start_preview(quest=quest, dialogue_tree=tree, presentation="player")
    session = advance_preview(session, action_id="choose", choice_id="ask:0")
    session = advance_preview(session, action_id="reply")
    assert session.values["trust"] == 0
    session = PreviewSession.model_validate_json(session.model_dump_json())
    done = advance_preview(session, action_id="end")
    assert done.status == "completed" and done.values["trust"] == 15
    assert [row.cursor for row in done.history if row.phase == "quest"] == [
        "meeting",
        "records",
        "hearing",
    ]
    assert done.action_seq == 3 and len(done.history) == 6
    assert preview_view(done)["text"] == "审讯室的门在你身后合上。"
    assert advance_preview(done, action_id="end").values["trust"] == 15


@pytest.mark.parametrize("terminal_option", [0, 1])
def test_actual_v3_compiler_endings_only_show_the_selected_route(terminal_option):
    import json

    from owcopilot.workflow.mechanic_ir_v2 import compile_mechanic_dialogue
    from tests.domain.test_creator_copy_contracts import player_case

    plan, compiled, prose = player_case()
    dialogue = compile_mechanic_dialogue(prose, compiled, plan, tree_id="runtime_v3")
    session = start_preview(
        quest=compiled.quest, dialogue_tree=dialogue.tree, presentation="player"
    )
    selected_count = 0
    while session.status in {"ready", "waiting_choice"}:
        view = preview_view(session)
        assert view["phase"] == "dialogue"
        option = None
        if view["choices"]:
            index = 0 if selected_count == 0 else terminal_option
            option = view["choices"][index]["id"]
            selected_count += 1
        session = advance_preview(session, action_id=f"v3_{session.action_seq}", choice_id=option)
    endings = ["The audience reads the report.", "You seal the report in your desk."]
    view = preview_view(session)
    assert session.status == "completed" and selected_count == 2
    assert view["text"] == view["outcome"] == endings[terminal_option]
    assert endings[1 - terminal_option] not in json.dumps(view)
    assert view["state_changes"]
    assert all(not row["id"].startswith("_owc_") for row in view["state_changes"])
