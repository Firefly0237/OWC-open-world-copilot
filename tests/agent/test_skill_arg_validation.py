"""Skill argument value validation/coercion (deepening T2).

``Skill.run`` validates required-presence AND now the *value* of each declared argument against
its type (and enum), coercing the common LLM slips (a stringified scalar) and raising a clear
``SkillError`` for a genuine mismatch. The error is fed back to the model as an observation, so
both the text and native tool-calling paths self-correct instead of crashing the handler.
"""

from __future__ import annotations

import pytest

from owcopilot.skills import (
    CostTier,
    SideEffect,
    Skill,
    SkillError,
    SkillParameter,
)


def _skill(*params: SkillParameter) -> Skill:
    return Skill(
        name="probe",
        description="Echo the (validated) args back.",
        cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY,
        handler=lambda **kw: {"echo": kw},
        parameters=params,
    )


# --------------------------------------------------------------------------- coercion (lenient)
@pytest.mark.parametrize(
    ("ptype", "given", "expected"),
    [
        ("integer", 5, 5),
        ("integer", "5", 5),  # model replied with a stringified int
        ("integer", 5.0, 5),  # integral float
        ("number", 1.5, 1.5),
        ("number", "1.5", 1.5),
        ("number", 2, 2),
        ("boolean", True, True),
        ("boolean", "true", True),
        ("boolean", "False", False),
        ("boolean", 1, True),
        ("boolean", 0, False),
        ("string", "hi", "hi"),
        ("string", 7, "7"),  # a number for a string field is coerced
    ],
)
def test_common_llm_slips_are_coerced(ptype: str, given: object, expected: object) -> None:
    skill = _skill(SkillParameter("x", ptype, "an arg"))
    assert skill.run({"x": given}) == {"echo": {"x": expected}}


# --------------------------------------------------------------------------- rejection (loud)
@pytest.mark.parametrize(
    ("ptype", "given"),
    [
        ("integer", "abc"),
        ("integer", True),  # bool must NOT satisfy integer despite being an int subclass
        ("integer", 1.5),  # non-integral float
        ("number", "x"),
        ("boolean", "maybe"),
        ("boolean", 2),
        ("string", True),  # a bool for a string field is a real mistake
        ("array", "not-a-list"),
        ("object", [1, 2]),
    ],
)
def test_genuine_type_mismatches_raise_skillerror(ptype: str, given: object) -> None:
    skill = _skill(SkillParameter("x", ptype, "an arg"))
    with pytest.raises(SkillError, match=r"argument 'x' expects " + ptype):
        skill.run({"x": given})


def test_array_and_object_pass_through_when_correct() -> None:
    skill = _skill(
        SkillParameter("items", "array", "a list"),
        SkillParameter("meta", "object", "a dict"),
    )
    out = skill.run({"items": [1, 2], "meta": {"k": "v"}})
    assert out == {"echo": {"items": [1, 2], "meta": {"k": "v"}}}


def test_unknown_declared_type_passes_through() -> None:
    # An unrecognised type name is permissive (matches the JSON-schema fallback), never a crash.
    skill = _skill(SkillParameter("x", "weird", "an arg"))
    assert skill.run({"x": {"any": "shape"}}) == {"echo": {"x": {"any": "shape"}}}


# --------------------------------------------------------------------------- enum
def test_enum_accepts_member_and_rejects_non_member() -> None:
    skill = _skill(SkillParameter("mode", "string", "mode", enum=("fast", "safe")))
    assert skill.run({"mode": "safe"}) == {"echo": {"mode": "safe"}}
    with pytest.raises(SkillError, match=r"argument 'mode' must be one of \['fast', 'safe'\]"):
        skill.run({"mode": "reckless"})


def test_enum_is_advertised_in_both_paths() -> None:
    skill = _skill(SkillParameter("mode", "string", "mode", enum=("fast", "safe")))
    # native function-calling schema
    schema = skill.openai_tool_schema()
    assert schema["function"]["parameters"]["properties"]["mode"]["enum"] == ["fast", "safe"]
    # text manifest signature advertises the same choices → parity between the two paths
    assert "mode: string (one of: fast, safe)" in skill.signature()


def test_validation_covers_both_dispatch_paths_via_run() -> None:
    # Both the text ReAct path and the native path dispatch through Skill.run / registry.run,
    # so validating here guards both. A required + typed arg: missing → error; mistyped → error.
    skill = _skill(SkillParameter("n", "integer", "a count", required=True))
    with pytest.raises(SkillError, match="missing required argument"):
        skill.run({})
    with pytest.raises(SkillError, match=r"argument 'n' expects integer"):
        skill.run({"n": "lots"})
    assert skill.run({"n": "3"}) == {"echo": {"n": 3}}
