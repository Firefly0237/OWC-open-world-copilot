from __future__ import annotations

from owcopilot.llm.telemetry import CallRecord, TelemetryCollector
from owcopilot.telemetry import deterministic_step, llm_step, summarize_workflow


def test_deterministic_step_has_zero_cost() -> None:
    step = deterministic_step("audit")
    workflow = summarize_workflow([step], budget_usd=0.0)

    assert step.calls == 0
    assert step.total_cost_usd == 0.0
    assert workflow.budget.used_usd == 0.0
    assert workflow.budget.over_budget is False


def test_llm_step_uses_gateway_telemetry_summary() -> None:
    telemetry = TelemetryCollector()
    telemetry.record(
        CallRecord(task="qa_answer", tier="cheap", input_tokens=1000, output_tokens=500)
    )

    step = llm_step("ask_lore", telemetry)

    assert step.calls == 1
    assert step.input_tokens == 1000
    assert step.output_tokens == 500
    assert step.total_cost_usd > 0.0


def test_cost_uses_env_price_override_and_flags_estimate(monkeypatch) -> None:
    rec = CallRecord(task="qa_answer", tier="cheap", input_tokens=1_000_000, output_tokens=0)
    # default illustrative cheap miss price is 0.30 / 1M → $0.30, and a real call on that
    # default-priced tier is flagged an estimate.
    assert rec.cost_usd == 0.30
    default_priced = TelemetryCollector()
    default_priced.record(CallRecord(task="qa", tier="cheap", input_tokens=1000, output_tokens=10))
    estimate = summarize_workflow([llm_step("ask", default_priced)]).budget
    assert estimate.cost_is_estimate is True

    # a studio sets its real rate → cost follows it and is no longer an estimate
    monkeypatch.setenv("OWCOPILOT_PRICE_CHEAP", "0.027,0.27,1.10")
    assert rec.cost_usd == 0.27  # 1M input tokens at the configured miss rate
    telemetry = TelemetryCollector()
    telemetry.record(CallRecord(task="qa", tier="cheap", input_tokens=1000, output_tokens=10))
    configured = summarize_workflow([llm_step("ask", telemetry)]).budget
    assert configured.cost_is_estimate is False


def test_partial_price_config_still_flags_unconfigured_tier_as_estimate(monkeypatch) -> None:
    # Regression: configuring ONE tier used to mark the whole figure as real, even when a call ran
    # on a different, still-default-priced tier. The flag must reflect the tiers actually used.
    monkeypatch.setenv("OWCOPILOT_PRICE_CHEAP", "0.027,0.27,1.10")
    monkeypatch.delenv("OWCOPILOT_PRICE_FRONTIER", raising=False)

    # only the configured `cheap` tier billed → real figure
    cheap_only = TelemetryCollector()
    cheap_only.record(CallRecord(task="qa", tier="cheap", input_tokens=1000, output_tokens=10))
    assert summarize_workflow([llm_step("ask", cheap_only)]).budget.cost_is_estimate is False

    # a `frontier` call priced from illustrative defaults → still an estimate despite cheap being on
    with_frontier = TelemetryCollector()
    with_frontier.record(CallRecord(task="qa", tier="cheap", input_tokens=1000, output_tokens=10))
    with_frontier.record(CallRecord(task="gen", tier="frontier", input_tokens=500, output_tokens=5))
    assert summarize_workflow([llm_step("gen", with_frontier)]).budget.cost_is_estimate is True


def test_client_cache_hits_are_exact_not_estimate() -> None:
    # An all-cache-hit run costs $0 exactly — never an "estimate" even with no prices configured.
    only_hits = TelemetryCollector()
    only_hits.record(
        CallRecord(task="qa", tier="cheap", input_tokens=0, output_tokens=0, cache_hit=True)
    )
    assert only_hits.cost_is_estimate is False
    assert summarize_workflow([llm_step("ask", only_hits)]).budget.cost_is_estimate is False


def test_deterministic_only_workflow_is_not_flagged_estimate() -> None:
    # a $0 deterministic step has an exact (zero) cost — never an "estimate".
    budget = summarize_workflow([deterministic_step("audit")]).budget
    assert budget.cost_is_estimate is False


def test_workflow_budget_detects_over_budget() -> None:
    telemetry = TelemetryCollector()
    telemetry.record(
        CallRecord(task="qa_answer", tier="cheap", input_tokens=1000, output_tokens=500)
    )
    workflow = summarize_workflow([llm_step("ask_lore", telemetry)], budget_usd=0.0)

    assert workflow.budget.used_usd > 0.0
    assert workflow.budget.remaining_usd < 0.0
    assert workflow.budget.over_budget is True
