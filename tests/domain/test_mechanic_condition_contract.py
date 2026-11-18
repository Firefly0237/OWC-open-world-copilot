"""Published condition examples exercise the actual parser and narrative compiler."""

from copy import deepcopy

import pytest

from owcopilot.logic.expr import LogicSyntaxError, parse_expr
from owcopilot.logic.semantics import evaluate, type_errors
from owcopilot.workflow.mechanic_ir_v2 import (
    MechanicDesignIRV2,
    compile_mechanic_design,
    compile_mechanic_dialogue,
    mechanic_prompt_contract,
)
from tests.domain.test_mechanic_ir_v2 import design_case


def _case():
    plan, quest, ir, prose = design_case()
    contract = mechanic_prompt_contract(plan, quest)
    published = contract["condition_expression_contract"]
    by_id = {variable["id"]: variable for variable in ir["variables"]}
    for variable in published["example_variables"]:
        by_id[variable["id"]] = deepcopy(variable)
    ir["variables"] = list(by_id.values())
    return plan, quest, ir, prose, contract


def _field_schema(schema, field):
    if field == "quest_precondition":
        return schema["properties"][field]
    model = "MechanicStageV2" if field == "precondition" else "MechanicTransitionV2"
    return schema["$defs"][model]["properties"][field]


def _assign(ir, field, expression):
    if field == "quest_precondition":
        ir[field] = expression
    elif field == "precondition":
        ir["stages"][0][field] = expression
    else:
        ir["stages"][0]["options"][0][field] = expression


@pytest.mark.parametrize("field", ["quest_precondition", "precondition", "extra_guard"])
def test_published_schema_and_prompt_examples_compile_and_complete_real_paths(field):
    plan, quest, original, prose, contract = _case()
    schema = contract["schema"]
    field_schema = _field_schema(schema, field)
    assert field_schema["type"] == "string"
    assert field_schema["description"]
    examples = field_schema["examples"]
    assert set(examples) == set(contract["condition_expression_contract"]["valid_examples"])
    defaults = {variable["id"]: variable["default"] for variable in original["variables"]}
    types = {variable["id"]: variable["type"] for variable in original["variables"]}
    for expression in examples:
        if expression:
            parsed = parse_expr(expression)
            assert type_errors(parsed, types) == []
            assert evaluate(parsed, defaults) is True
        ir = deepcopy(original)
        _assign(ir, field, expression)
        compiled = compile_mechanic_design(ir, plan, quest, require_distinct=True)
        dialogue = compile_mechanic_dialogue(prose, compiled, plan, tree_id="condition_contract")
        # Dialogue compilation invokes the actual path stepper and refuses blocked routes.
        assert dialogue.tree.root_node in dialogue.tree.nodes
        assert compiled.compiler_version == "mechanic-ir-v2.1"


@pytest.mark.parametrize("field", ["quest_precondition", "precondition", "extra_guard"])
def test_advertised_invalid_examples_remain_invalid_to_the_real_compiler(field):
    plan, quest, original, _, contract = _case()
    for example in contract["condition_expression_contract"]["invalid_examples"]:
        ir = deepcopy(original)
        _assign(ir, field, example["value"])
        # A JSON string can satisfy the outer schema and still violate its DSL contract.
        MechanicDesignIRV2.model_validate(ir)
        with pytest.raises(LogicSyntaxError):
            compile_mechanic_design(ir, plan, quest)


@pytest.mark.parametrize(
    "expression",
    [
        "signal == 0 且 trust == 0",
        "已完成信号阶段；signal 为 0 或 1，trust 为 0",
    ],
)
def test_observed_natural_language_stage_conditions_are_not_translated_or_accepted(expression):
    plan, quest, ir, _, _ = _case()
    ir["variables"].append({"id": "signal", "type": "int", "default": 0})
    ir["stages"][0]["precondition"] = expression
    with pytest.raises(LogicSyntaxError):
        compile_mechanic_design(ir, plan, quest)


@pytest.mark.parametrize(
    "expression,error",
    [
        ("previous_stage_done == true", "未声明"),
        ("admitted > 0", "类型"),
    ],
)
def test_valid_dsl_syntax_still_requires_declared_and_typed_game_state(expression, error):
    plan, quest, ir, _, _ = _case()
    parse_expr(expression)
    ir["stages"][0]["precondition"] = expression
    with pytest.raises(ValueError, match=error):
        compile_mechanic_design(ir, plan, quest)
