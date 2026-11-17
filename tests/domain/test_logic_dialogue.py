"""N4 dialogue loop: DialogueChoice.effects + walk/explore + Branch.effects + byte stability.

Tests assemble inputs and assert on production code (walk_dialogue_tree / explore_dialogue_tree /
simulate_quest / explore_quest / audit rules / model serialization) — no business logic is copied.
"""

from __future__ import annotations

import json

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.dialogue_rules import DialogueChoiceConditionRule
from owcopilot.content.models import (
    ContentBundle,
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
from owcopilot.logic import (
    explore_dialogue_tree,
    explore_quest,
    simulate_quest,
    walk_dialogue_tree,
)


def _tree() -> DialogueTree:
    """Root: two choices — [0] gated off by default, [1] open and sets `helped` true."""
    return DialogueTree(
        id="dlg_gate",
        quest_id="q_gate",
        root_node="n0",
        nodes={
            "n0": DialogueNode(
                id="n0",
                text="要帮忙吗？",
                choices=[
                    DialogueChoice(text="(冷漠离开)", next_node="n_end", condition="cold"),
                    DialogueChoice(
                        text="我来帮你。",
                        next_node="n_end",
                        effects=[Effect(var="helped", op="set", value=True)],
                    ),
                ],
            ),
            "n_end": DialogueNode(id="n_end", text="……"),
        },
    )


def _gated_quest() -> Quest:
    """Stage s2's precondition reads the flag the dialogue choice sets."""
    return Quest(
        id="q_gate",
        title="门后",
        stages=[QuestStage(id="s1", summary="谈话"), QuestStage(id="s2", summary="进门")],
        logic=QuestLogic(
            variables=[LogicVar(id="helped", type="bool", default=False)],  # type: ignore[arg-type]
            stage_logic=[StageLogic(stage_id="s2", precondition="helped")],
        ),
    )


# --- walk: conditions gate, effects apply, selectors address ------------------------------------
def test_walk_applies_choice_effects_and_default_pick() -> None:
    walk = walk_dialogue_tree(_tree())
    # choice 0 is gated by `cold` (seeded False) -> first satisfiable is choice 1
    assert walk.status == "completed"
    assert walk.steps[0].chosen == "n0:1"
    assert walk.steps[0].effects == ["helped set True"]
    assert walk.final_state["helped"] is True


def test_walk_queued_selector_and_initial_state_override() -> None:
    walk = walk_dialogue_tree(_tree(), choices=["n0:0"], initial_state={"cold": True})
    assert walk.steps[0].chosen == "n0:0"
    assert walk.final_state.get("helped") is not True


def test_walk_dangling_and_cycle_and_deadlock() -> None:
    dangling = DialogueTree(
        id="d", root_node="a", nodes={"a": DialogueNode(id="a", next_node="ghost")}
    )
    assert walk_dialogue_tree(dangling).status == "dangling"
    cyclic = DialogueTree(
        id="c",
        root_node="a",
        nodes={
            "a": DialogueNode(id="a", next_node="b"),
            "b": DialogueNode(id="b", next_node="a"),
        },
    )
    assert walk_dialogue_tree(cyclic).status == "cycle"
    dead = DialogueTree(
        id="x",
        root_node="a",
        nodes={
            "a": DialogueNode(id="a", choices=[DialogueChoice(text="never", condition="false")])
        },
    )
    assert walk_dialogue_tree(dead).status == "deadlock"


# --- the end-to-end closure: dialogue choice -> flag -> quest path changes ----------------------
def test_dialogue_choice_gates_quest_path() -> None:
    quest = _gated_quest()
    # Without the dialogue, `helped` seeds False -> s2's precondition blocks the quest.
    blocked = simulate_quest(quest)
    assert blocked.status == "blocked"
    # Walk the tree (choice sets helped=True), feed its final state into the simulation.
    state = walk_dialogue_tree(_tree()).final_state
    run = simulate_quest(quest, initial_state=state)
    assert run.status == "completed" and run.path == ["s1", "s2"]


def test_explore_dialogue_tree_enumerates_both_arms() -> None:
    exploration = explore_dialogue_tree(_tree(), initial_state={"cold": True})
    # Both choices satisfiable now -> two distinct endings with distinct states.
    assert sorted(exploration.covered_choices) == ["n0:0", "n0:1"]
    finals = [e.final_state.get("helped") for e in exploration.endings]
    assert True in finals and (False in finals or None in finals)
    assert exploration.dead_choices == []
    gated = explore_dialogue_tree(_tree())  # cold stays False -> choice 0 never satisfiable
    assert gated.dead_choices == ["n0:0"]


# --- Branch.effects now apply in simulate AND explore (with witness replay intact) --------------
def _branchy_quest() -> Quest:
    return Quest(
        id="q_br",
        title="岔路",
        stages=[QuestStage(id="s1", summary="选边"), QuestStage(id="s2", summary="结算")],
        logic=QuestLogic(
            variables=[LogicVar(id="rep", type="int", default=0)],  # type: ignore[arg-type]
            stage_logic=[StageLogic(stage_id="s2", precondition="rep > 0")],
            branches=[
                {  # type: ignore[list-item]
                    "id": "b_help",
                    "from_stage": "s1",
                    "to_stage": "s2",
                    "effects": [{"var": "rep", "op": "inc", "value": 2}],
                },
                {"id": "b_skip", "from_stage": "s1", "to_stage": "s2"},  # type: ignore[list-item]
            ],
        ),
    )


def test_simulate_applies_branch_effects() -> None:
    quest = _branchy_quest()
    helped = simulate_quest(quest, choices=["b_help"])
    assert helped.status == "completed"
    assert helped.final_state["rep"] == 2
    assert "rep inc 2" in helped.steps[0].effects
    skipped = simulate_quest(quest, choices=["b_skip"])  # no effects -> s2 precondition fails
    assert skipped.status == "blocked" and skipped.final_state["rep"] == 0


def test_explore_diverges_per_branch_and_witnesses_replay() -> None:
    report = explore_quest(_branchy_quest())
    # b_help's effect makes s2 passable on that arm; b_skip's arm blocks -> a dead end exists.
    assert any(d.reason == "blocked" for d in report.dead_ends)
    reachable = [o for o in report.outcomes if o.reachable]
    assert reachable, report
    for outcome in reachable:  # anti-divergence: every witness replays through the simulator
        run = simulate_quest(_branchy_quest(), choices=list(outcome.witness))
        assert run.status == "completed"


# --- serialization byte-stability sentinel -------------------------------------------------------
def test_empty_effects_do_not_serialize() -> None:
    plain = DialogueChoice(text="hi")
    assert "effects" not in plain.model_dump(mode="json", exclude_none=True)
    assert "effects" not in json.loads(plain.model_dump_json())
    rich = DialogueChoice(text="hi", effects=[Effect(var="x", op="set", value=True)])
    dumped = rich.model_dump(mode="json")
    assert dumped["effects"] == [{"var": "x", "op": "set", "value": True}]
    # round-trip: a dump without the key validates back to the default empty list
    assert DialogueChoice.model_validate({"text": "hi"}).effects == []


def test_bundle_with_tree_but_no_effects_is_byte_stable() -> None:
    tree = DialogueTree(
        id="t",
        root_node="a",
        nodes={"a": DialogueNode(id="a", choices=[DialogueChoice(text="ok")])},
    )
    payload = ContentBundle(dialogue_trees={"t": tree}).model_dump(mode="json", exclude_none=True)
    assert "effects" not in json.dumps(payload)


# --- A1 (localization remediation): text_key omission mirrors the effects/style_rubrics sentinel --
def test_empty_text_key_does_not_serialize() -> None:
    """A DialogueNode/DialogueChoice with the default (empty) text_key never emits the key, so
    every dialogue tree written before A1 keeps hashing/exporting byte-identically."""
    plain_choice = DialogueChoice(text="hi")
    assert "text_key" not in plain_choice.model_dump(mode="json", exclude_none=True)
    assert "text_key" not in json.loads(plain_choice.model_dump_json())
    plain_node = DialogueNode(id="n", text="hi")
    assert "text_key" not in plain_node.model_dump(mode="json", exclude_none=True)
    assert "text_key" not in json.loads(plain_node.model_dump_json())

    keyed_choice = DialogueChoice(text="hi", text_key="dlg.n.c0")
    assert keyed_choice.model_dump(mode="json")["text_key"] == "dlg.n.c0"
    keyed_node = DialogueNode(id="n", text="hi", text_key="dlg.n")
    assert keyed_node.model_dump(mode="json")["text_key"] == "dlg.n"

    # round-trip: a dump without the key validates back to the default empty string
    assert DialogueChoice.model_validate({"text": "hi"}).text_key == ""
    assert DialogueNode.model_validate({"id": "n"}).text_key == ""


def test_bundle_with_tree_but_no_text_key_is_byte_stable() -> None:
    tree = DialogueTree(
        id="t",
        root_node="a",
        nodes={"a": DialogueNode(id="a", text="hi", choices=[DialogueChoice(text="ok")])},
    )
    payload = ContentBundle(dialogue_trees={"t": tree}).model_dump(mode="json", exclude_none=True)
    assert "text_key" not in json.dumps(payload)


# --- product entry: the dialogue CLI (blocker fix — walk/explore reachable by users) --------------
def test_cli_dialogue_walk_and_composed_simulate(tmp_path, capsys) -> None:
    import json as _json

    from owcopilot.cli.main import main
    from owcopilot.content.store import ContentStore

    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            quests={"q_gate": _gated_quest()},
            dialogue_trees={"dlg_gate": _tree()},
        )
    )
    # dialogue-walk: deterministic walk with effects applied
    assert main(["dialogue-walk", "--content-root", str(root), "dlg_gate"]) == 0
    walk = _json.loads(capsys.readouterr().out)
    assert walk["status"] == "completed" and walk["final_state"]["helped"] is True
    # dialogue-walk --explore: enumeration
    assert main(["dialogue-walk", "--content-root", str(root), "dlg_gate", "--explore"]) == 0
    exploration = _json.loads(capsys.readouterr().out)
    assert exploration["dead_choices"] == ["n0:0"]  # cold seeds False
    # the composition contract as ONE command: dialogue outcome gates the quest
    assert (
        main(
            [
                "quest-simulate",
                "--content-root",
                str(root),
                "q_gate",
                "--dialogue-tree",
                "dlg_gate",
            ]
        )
        == 0
    )
    run = _json.loads(capsys.readouterr().out)
    assert run["status"] == "completed" and run["dialogue_walk"]["status"] == "completed"
    # missing tree id -> guided error
    assert main(["dialogue-walk", "--content-root", str(root), "ghost"]) == 2
    assert "不存在" in capsys.readouterr().err


# --- audit: effect vars must be declared in the bound quest's logic ------------------------------
def _audit_issues(tree: DialogueTree, quest: Quest) -> list[str]:
    bundle = ContentBundle(quests={quest.id: quest}, dialogue_trees={tree.id: tree})
    ctx = AuditContext.from_bundle(bundle)
    return [issue.message for issue in DialogueChoiceConditionRule().check(ctx)]


def test_audit_flags_undeclared_effect_var_and_passes_declared() -> None:
    quest = _gated_quest()
    good = _tree()  # helped IS declared in q_gate's logic
    assert not any("选中后会修改变量" in m for m in _audit_issues(good, quest))
    bad = _tree()
    bad.nodes["n0"].choices[1].effects.append(Effect(var="ghost_var", op="set", value=True))
    messages = _audit_issues(bad, quest)
    assert any("选中后会修改变量 'ghost_var'" in m for m in messages)
