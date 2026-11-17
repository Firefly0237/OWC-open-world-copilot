"""E-1: the general error-code vocabulary and the classifier that maps real skill failures to it.

Every case GENERATES the exception through the production raise site (registry dispatch, argument
validation, the WRITES_CANON ban, execution scoping, read_observation) rather than hand-crafting a
message string — so the classifier is validated against the actual wording it keys on, and a change
to that wording fails here loudly.
"""

from __future__ import annotations

import pytest

from owcopilot.skills import (
    CostTier,
    SideEffect,
    Skill,
    SkillError,
    SkillParameter,
    SkillRegistry,
    default_skill_registry,
    scoped_registry,
)
from owcopilot.skills.errors import ErrorCode, ExternalSkillError, classify_error, error_envelope
from owcopilot.skills.observation_overflow import bind_overflow, read_observation


def _skill(name: str, *, side_effect: SideEffect = SideEffect.READ_ONLY, parameters=()) -> Skill:
    return Skill(
        name=name,
        description=f"{name} skill.",
        cost_tier=CostTier.DETERMINISTIC,
        side_effect=side_effect,
        handler=lambda **_: {"ok": True},
        parameters=parameters,
    )


def _raised(call) -> SkillError:
    with pytest.raises(SkillError) as excinfo:
        call()
    return excinfo.value


def test_unknown_skill_name_is_not_found() -> None:
    exc = _raised(lambda: SkillRegistry().run("nope", {}))
    assert classify_error(exc) is ErrorCode.NOT_FOUND


def test_out_of_scope_skill_is_not_found() -> None:
    base = SkillRegistry()
    base.register(_skill("allowed"))
    base.register(_skill("secret"))
    exc = _raised(lambda: scoped_registry(base, {"allowed"}).run("secret", {}))
    assert classify_error(exc) is ErrorCode.NOT_FOUND


def test_missing_required_argument_is_invalid_arg() -> None:
    skill = _skill("echo", parameters=(SkillParameter("v", "string", "value", required=True),))
    exc = _raised(lambda: skill.run({}))
    assert classify_error(exc) is ErrorCode.INVALID_ARG


def test_wrong_type_argument_is_invalid_arg() -> None:
    skill = _skill("count", parameters=(SkillParameter("n", "integer", "count", required=True),))
    exc = _raised(lambda: skill.run({"n": "not-a-number"}))
    assert classify_error(exc) is ErrorCode.INVALID_ARG


def test_writes_canon_auto_invoke_ban_is_conflict() -> None:
    skill = _skill("publish", side_effect=SideEffect.WRITES_CANON)
    exc = _raised(lambda: skill.run({}))
    assert classify_error(exc) is ErrorCode.CONFLICT


def test_unknown_obs_id_is_not_found_but_out_of_run_is_conflict() -> None:
    with bind_overflow({}):
        exc_unknown = _raised(lambda: read_observation("obs-9-9"))
    assert classify_error(exc_unknown) is ErrorCode.NOT_FOUND
    # No active run bound → a state problem, not a lookup miss.
    exc_out_of_run = _raised(lambda: read_observation("obs-0-0"))
    assert classify_error(exc_out_of_run) is ErrorCode.CONFLICT


def test_non_skill_exceptions_map_by_type() -> None:
    assert classify_error(FileNotFoundError("issue not found: q1")) is ErrorCode.NOT_FOUND
    assert classify_error(ValueError("bad change_type")) is ErrorCode.INVALID_ARG
    assert classify_error(KeyError("change_type")) is ErrorCode.INVALID_ARG
    assert classify_error(TypeError("wrong shape")) is ErrorCode.INVALID_ARG
    assert classify_error(RuntimeError("boom")) is ErrorCode.INTERNAL


def test_checked_external_error_keeps_typed_replay_policy() -> None:
    exc = ExternalSkillError(
        "sanitized boundary failure",
        error_code=ErrorCode.OUTCOME_UNKNOWN,
        retryable=False,
        outcome_unknown=True,
    )

    assert classify_error(exc) is ErrorCode.OUTCOME_UNKNOWN
    assert error_envelope(
        classify_error(exc),
        f"Error: {exc}",
        retryable=exc.retryable,
        outcome_unknown=exc.outcome_unknown,
    ) == {
        "status": "error",
        "error_code": "OUTCOME_UNKNOWN",
        "message": "Error: sanitized boundary failure",
        "retryable": False,
        "outcome_unknown": True,
    }


def test_error_envelope_shape() -> None:
    assert error_envelope(ErrorCode.NOT_FOUND, "Error: unknown skill 'x'") == {
        "status": "error",
        "error_code": "NOT_FOUND",
        "message": "Error: unknown skill 'x'",
    }


def test_builtin_optional_defaults_match_execution_and_tool_schema() -> None:
    registry = default_skill_registry(content_root="unused-content-root")
    expected = {
        "list_issues": {"severity": None, "rule_code": None, "status": None},
        "build_context_pack": {"budget_tokens": 800},
        "search_references": {"budget_tokens": 1000},
        "flag_xref": {"flag": None},
        "impact_of": {"max_depth": 2},
        "propose_fix": {"max_candidates": 3},
        "quality_harness": {"max_issues": 5, "propose_fixes": True},
    }
    required_args = {
        "build_context_pack": {"query": "q"},
        "search_references": {"query": "q"},
        "impact_of": {"changes": []},
        "propose_fix": {"issue_id": "i1"},
    }

    for skill_name, defaults in expected.items():
        skill = registry.get(skill_name)
        normalised = skill.normalise_args(required_args.get(skill_name, {}))
        schema = skill.openai_tool_schema()["function"]["parameters"]["properties"]
        for parameter, value in defaults.items():
            assert normalised[parameter] == value
            assert schema[parameter]["default"] == value


def test_error_code_vocabulary_is_the_agreed_set() -> None:
    # The recovery layer in the agent system prompt keys off exactly these values.
    assert {c.value for c in ErrorCode} == {
        "NOT_FOUND",
        "INVALID_ARG",
        "EMPTY_RESULT",
        "CONFLICT",
        "NO_PROGRESS",
        "BUDGET_EXCEEDED",
        "RUN_CONTRACT_MISMATCH",
        "EXTERNAL_TIMEOUT",
        "EXTERNAL_UNAVAILABLE",
        "EXTERNAL_PROTOCOL",
        "OUTCOME_UNKNOWN",
        "INTERNAL",
    }
