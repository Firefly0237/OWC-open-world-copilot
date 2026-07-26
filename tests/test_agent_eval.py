"""Tests for the JD-dimension Agent-Eval scorecard (``evaluation/agent_eval.py``).

The scorecard is an aggregation of existing deterministic ``$0`` signals, so these assert it is
deterministic, covers the five JD dimensions with provenance, and that each dimension actually
reflects its underlying evaluator (not a hard-coded constant).

W1-2 标注：工具调用效果维度的 F1=1.0 是离线 fixture（OfflineGoalAwareChatModel）产物，不代表真实
LLM 能力；reliability 字段的 pass_hat_k 在这里恒等于 pass@1（全部 39 场景 n_trials=1）。两者均不得
作为对外材料声称的数字，详见 ``evaluation/agent_eval.py``/``evaluation/reliability.py`` 模块头。
"""

from __future__ import annotations

from owcopilot.assist.calibration import _wilson_interval
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.evaluation.agent_eval import (
    AGENT_EVAL_FLOOR,
    _injection_detection_rate,
    run_jd_dimension_scorecard,
)

_EXPECTED_DIMENSIONS = {
    "任务完成",
    "角色一致性",
    "叙事合理性",
    "工具调用效果",
    "安全性",
}


def test_scorecard_covers_the_five_jd_dimensions_with_provenance() -> None:
    card = run_jd_dimension_scorecard()
    assert {d.dimension for d in card.dimensions} == _EXPECTED_DIMENSIONS
    for dim in card.dimensions:
        assert 0.0 <= dim.score <= 1.0
        assert dim.source  # every dimension names the evaluator it aggregates
        assert dim.measures  # and states honestly what it does / does not prove
        assert dim.sample_size >= 1


def test_scorecard_is_deterministic() -> None:
    first = run_jd_dimension_scorecard()
    second = run_jd_dimension_scorecard()
    assert first.model_dump() == second.model_dump()


def test_scorecard_overall_clears_the_regression_floor() -> None:
    card = run_jd_dimension_scorecard()
    assert card.overall >= AGENT_EVAL_FLOOR
    # overall is the mean of the five dimension scores
    assert card.overall == round(sum(d.score for d in card.dimensions) / len(card.dimensions), 4)


def test_tool_dimension_reflects_real_evaluation_not_a_constant() -> None:
    """工具调用效果 must come from the actual gold-scenario F1, sized to the scenario count."""
    card = run_jd_dimension_scorecard()
    tool = next(d for d in card.dimensions if d.dimension == "工具调用效果")
    assert tool.sample_size == 10  # the 10 gold ReAct scenarios
    assert "evaluate_tool_selection" in tool.source


def test_injection_probe_needs_both_detection_and_no_over_fire() -> None:
    """安全性 is 1.0 only when the injected surface fires AND the benign one stays silent."""
    runner = AuditRunner(build_default_rule_registry())
    assert _injection_detection_rate(runner) == 1.0


def test_reliability_report_wires_the_deterministic_scenarios() -> None:
    """The new .reliability field aggregates the 39 deterministic binary scenarios (τ-bench pass^k),
    single-trial so it honestly collapses to pass@1, with the pooled Wilson CI reused verbatim."""
    rel = run_jd_dimension_scorecard().reliability
    assert rel.n_scenarios == 39  # 25 seeded + 10 gold ReAct + 3 repair worlds + 1 injection probe
    assert rel.trials == 39  # each deterministic scenario is single-trial (n_trials=1)
    assert set(rel.pass_hat_k) == {1}  # only k=1 is defined at n_trials=1
    assert rel.pass_hat_k[1] == rel.success_rate  # pass^1 == pass@1 == pooled success rate
    assert rel.success_rate_ci == _wilson_interval(rel.successes, rel.trials)  # reuse, not a copy
    assert "n_trials=1" in rel.note  # honest about the offline collapse


def test_reliability_is_additive_not_folded_into_overall() -> None:
    """Byte-behavioral proof that reliability is a SEPARATE field: overall stays the plain mean of
    the five dimension scores and the dump gains exactly one key, nothing else shifts."""
    card = run_jd_dimension_scorecard()
    assert card.overall == round(sum(d.score for d in card.dimensions) / len(card.dimensions), 4)
    assert set(card.model_dump()) == {"dimensions", "overall", "note", "reliability"}
