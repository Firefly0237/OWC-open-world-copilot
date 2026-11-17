"""Controlled development designs, exercised against the real pure narrative stepper."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from owcopilot.content.models import Effect
from owcopilot.logic.stepper import advance_preview, preview_view, start_preview
from owcopilot.workflow.decision_contract import validate_decisions
from owcopilot.workflow.mechanic_ir_v2 import (
    QuestDraftV2,
    compile_mechanic_design,
    compile_mechanic_dialogue,
    materialize_quest_draft,
    read_compilation_quest,
    seal_compilation_quest,
    validate_compiled_playability,
    verify_mechanic_compilation,
)


def design_case():
    """Four stages, two decisions, a genuine split/merge and delayed game state."""
    draft = {
        "title": "Development input: investigation",
        "objective": "Investigate before a public decision.",
        "stages": [
            {"id": "arrival", "summary": "Choose how to investigate"},
            {"id": "records", "summary": "Read the records"},
            {"id": "witness", "summary": "Speak with the witness"},
            {"id": "hearing", "summary": "Choose a response at the hearing"},
        ],
    }
    plan = {
        "premise": "Development input, not an author acceptance or model result",
        "beats": ["Investigate", "Decide"],
        "events": [
            {"id": "investigate", "summary": "Investigate"},
            {"id": "hear", "summary": "Hear"},
        ],
        "decisions": [
            {
                "id": "method",
                "event_id": "investigate",
                "options": ["Records", "Witness"],
                "consequences": ["Evidence in hand", "Trust in a witness"],
            },
            {
                "id": "response",
                "event_id": "hear",
                "options": ["Reveal", "Keep private"],
                "consequences": ["Reputation rises", "Reputation falls"],
            },
        ],
    }
    ir = {
        "purpose": "A nontrivial forward split and merge with two decisions",
        "variables": [
            {"id": "trust", "type": "int", "default": 0},
            {"id": "coins", "type": "int", "default": 0},
            {"id": "fame", "type": "int", "default": 0},
            {"id": "evidence", "type": "bool", "default": False},
            {"id": "admitted", "type": "bool", "default": True},
        ],
        "stages": [
            {
                "stage_id": "arrival",
                "purpose": "Choose an investigation route",
                "decision_id": "method",
                "options": [
                    {
                        "purpose": "Gather evidence and a small reward",
                        "to_stage": "records",
                        "extra_guard": "admitted == true",
                        "effects": [
                            {"var": "trust", "op": "inc", "value": 1},
                            {"var": "coins", "op": "inc", "value": 2},
                            {"var": "evidence", "value": True},
                        ],
                    },
                    {
                        "purpose": "Trust the witness instead",
                        "to_stage": "witness",
                        "effects": [
                            {"var": "trust", "op": "inc", "value": 3},
                            {"var": "coins", "op": "inc", "value": 1},
                        ],
                    },
                ],
            },
            {
                "stage_id": "records",
                "purpose": "Carry the actual records route into the shared hearing",
                "automatic": {"purpose": "Gather the route", "to_stage": "hearing"},
            },
            {
                "stage_id": "witness",
                "purpose": "Carry the alternative route without merging facts",
                "automatic": {"purpose": "Gather the route", "to_stage": "hearing"},
            },
            {
                "stage_id": "hearing",
                "purpose": "Settle the selected route and its reputation consequence",
                "precondition": "trust > 0",
                "decision_id": "response",
                "options": [
                    {
                        "purpose": "Make the report public",
                        "outcome": "The report is public.",
                        "effects": [{"var": "fame", "op": "inc", "value": 2}],
                    },
                    {
                        "purpose": "Keep the report private",
                        "outcome": "The report remains private.",
                        "effects": [{"var": "fame", "op": "dec", "value": 1}],
                    },
                ],
            },
        ],
    }
    prose = {
        "stages": [
            {
                "stage_id": "arrival",
                "text": "Where will you investigate?",
                "options": [{"text": "Read records"}, {"text": "Ask a witness"}],
            },
            {"stage_id": "records", "text": "You inspect the written records."},
            {"stage_id": "witness", "text": "You listen to the witness."},
            {
                "stage_id": "hearing",
                "text": "How will you present your report?",
                "options": [
                    {"text": "Reveal", "response_text": "You commit to a public report."},
                    {"text": "Keep private"},
                ],
            },
        ]
    }
    return plan, materialize_quest_draft(draft, quest_id="q_dev"), ir, prose


@pytest.mark.parametrize("forbidden", ["logic", "stage_logic", "metadata"])
def test_quest_draft_rejects_mechanics_and_never_defaults_to_author_approval(forbidden):
    raw = {
        "title": "Development draft",
        "objective": "An objective",
        "stages": [{"id": "s", "summary": "A stage"}],
    }
    with pytest.raises(ValidationError):
        QuestDraftV2.model_validate({**raw, forbidden: {}})
    quest = materialize_quest_draft(raw, quest_id="host_q")
    assert quest.logic is None and quest.origin.value == "ai_draft"
    assert quest.review_status.value == "pending_review"
    assert "requirements_v2" not in quest.metadata
    with pytest.raises(ValueError, match="provenance"):
        materialize_quest_draft(raw, quest_id="host_q", provenance={"review_status": "approved"})


def test_compiler_owns_all_indices_selectors_and_real_split_merge_settlement_once():
    plan, quest, ir, prose = design_case()
    compiled = compile_mechanic_design(ir, plan, quest, require_distinct=True)
    repeated = compile_mechanic_design(ir, plan, quest, require_distinct=True)
    assert compiled.manifest == repeated.manifest
    assert compiled.decisions is not None
    assert compiled.decisions.options[0].effect_indices == [0, 1, 2]
    assert all("unset" not in branch.condition for branch in compiled.quest.logic.branches)
    assert compiled.manifest["semantic_status"] == "unknown"
    dialogue = compile_mechanic_dialogue(prose, compiled, plan, tree_id="tree_dev")
    assert dialogue.playability["completed_path_count"] == 4
    assert dialogue.playability["covered_option_count"] == 4
    states = [path["final_game_state"] for path in dialogue.playability["paths"]]
    assert {
        (state["trust"], state["coins"], state["evidence"], state["fame"]) for state in states
    } == {(1, 2, True, 2), (1, 2, True, -1), (3, 1, False, 2), (3, 1, False, -1)}
    session = start_preview(quest=compiled.quest, dialogue_tree=dialogue.tree)
    original = dict(session.values)
    while session.phase == "dialogue":
        view = preview_view(session)
        choice_id = view["choices"][0]["id"] if view["choices"] else None
        session = advance_preview(
            session, action_id=f"dialogue_{session.action_seq}", choice_id=choice_id
        )
        assert session.values["trust"] == original["trust"]
        assert session.values["coins"] == original["coins"]
    while session.status != "completed":
        view = preview_view(session)
        branch_id = view["choices"][0]["id"]
        action_id = f"quest_{session.action_seq}"
        session = advance_preview(session, action_id=action_id, choice_id=branch_id)
        replayed = advance_preview(session, action_id=action_id, choice_id=branch_id)
        assert replayed.values == session.values and replayed.action_seq == session.action_seq
    assert (
        session.values["trust"] == 1
        and session.values["coins"] == 2
        and session.values["fame"] == 2
    )
    assert session.outcome == "The report is public."
    assert quest.logic is None  # The input content artifact was not modified in place.


@pytest.mark.parametrize(
    "fault", ["dynamic_guard", "loop", "unknown_effect", "disabled_guard", "redundant_indices"]
)
def test_unsupported_or_invalid_model_mechanics_fail_instead_of_fallback(fault):
    plan, quest, ir, _ = design_case()
    if fault == "dynamic_guard":
        ir["stages"][3]["options"][0]["extra_guard"] = "trust > 0"
        reason = "动态option guard"
    elif fault == "loop":
        ir["stages"][1]["automatic"]["to_stage"] = "arrival"
        reason = "循环"
    elif fault == "unknown_effect":
        ir["stages"][0]["options"][0]["effects"][0]["var"] = "undefined"
        reason = "未声明"
    elif fault == "disabled_guard":
        ir["stages"][0]["options"][0]["extra_guard"] = "admitted == false"
        reason = "声明初态不可用"
    else:
        ir["stages"][0]["options"][0]["effect_indices"] = [0]
        reason = "Extra inputs"
    with pytest.raises(ValueError, match=reason):
        compile_mechanic_design(ir, plan, quest)


def test_independent_runtime_check_rejects_tampered_hash_and_early_duplicate_game_effects():
    plan, quest, ir, prose = design_case()
    compiled = compile_mechanic_design(ir, plan, quest)
    changed = compiled.model_dump(mode="json")
    changed["quest"]["logic"]["branches"][0]["effects"][0]["value"] = 99
    with pytest.raises(ValueError, match="manifest已改变"):
        verify_mechanic_compilation(changed, plan)
    dialogue = compile_mechanic_dialogue(prose, compiled, plan, tree_id="tree_dev")
    tree = dialogue.tree.model_copy(deep=True)
    tree.nodes[tree.root_node].choices[0].effects.append(Effect(var="trust", op="inc", value=1))
    with pytest.raises(ValueError, match="selector|提前"):
        validate_compiled_playability(
            plan, compiled.quest, compiled.decisions, tree, dialogue.choices
        )
    staged = compiled.quest.model_copy(deep=True)
    staged.logic.stage_logic[0].effects_on_complete.append(Effect(var="trust", op="inc", value=1))
    with pytest.raises(ValueError, match="stage effects"):
        validate_compiled_playability(
            plan, staged, compiled.decisions, dialogue.tree, dialogue.choices
        )
    blocked = deepcopy(ir)
    blocked["stages"][3]["precondition"] = "trust > 100"
    impossible = compile_mechanic_design(blocked, plan, quest)
    with pytest.raises(ValueError, match="stepper路径无法完成"):
        compile_mechanic_dialogue(prose, impossible, plan, tree_id="tree_blocked")


def test_distinct_outcomes_are_checked_at_actual_reachable_prestate_not_only_defaults():
    plan, quest, ir, prose = design_case()
    # At the isolated default fame=0, inc(0) and set(1) differ. Both real routes
    # set fame=1 first, at which point the later alternatives are identical.
    for option in ir["stages"][0]["options"]:
        option["effects"].append({"var": "fame", "value": 1})
    ir["stages"][3]["options"][0]["effects"] = [{"var": "fame", "op": "inc", "value": 0}]
    ir["stages"][3]["options"][1]["effects"] = [{"var": "fame", "value": 1}]
    compiled = compile_mechanic_design(ir, plan, quest, require_distinct=True)
    assert compiled.validated_contract["status"] == "validated"
    with pytest.raises(ValueError, match="实际可达决策前态"):
        compile_mechanic_dialogue(prose, compiled, plan, tree_id="collapsed_consequences")


@pytest.mark.parametrize("changed", [None, "formal_quest", "raw_ir", "manifest", "plan"])
def test_compilation_lineage_survives_real_contract_port_and_rejects_tampering(changed):
    plan, quest, ir, prose = design_case()
    compiled = compile_mechanic_design(ir, plan, quest, require_distinct=True)
    sealed = seal_compilation_quest(compiled)
    # This is the same production validated-contract serialization boundary,
    # rather than a process cache or an unbound fixture report.
    contract = validate_decisions(plan, sealed, compiled.decisions, require_distinct=True)
    actual = deepcopy(contract["quest"])
    if changed == "formal_quest":
        actual["objective"] = "Tampered after compilation"
    elif changed == "raw_ir":
        actual["metadata"]["mechanic_compilation_v2"]["raw_ir"]["purpose"] = "Tampered source"
    elif changed == "manifest":
        actual["metadata"]["mechanic_compilation_v2"]["manifest"]["quest_hash"] = "0" * 64
    elif changed == "plan":
        plan["premise"] = "Different actual input plan"
    if changed is not None:
        with pytest.raises(ValueError, match="不一致"):
            read_compilation_quest(actual, plan)
        return
    recovered = read_compilation_quest(actual, plan)
    assert recovered.manifest == compiled.manifest
    assert "mechanic_compilation_v2" not in recovered.quest.metadata
    dialogue = compile_mechanic_dialogue(prose, recovered, plan, tree_id="through_actual_port")
    assert dialogue.playability["completed_path_count"] == 4
    assert dialogue.playability["require_distinct"] is True
