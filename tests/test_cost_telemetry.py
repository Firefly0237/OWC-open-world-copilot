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


def test_workflow_budget_detects_over_budget() -> None:
    telemetry = TelemetryCollector()
    telemetry.record(
        CallRecord(task="qa_answer", tier="cheap", input_tokens=1000, output_tokens=500)
    )
    workflow = summarize_workflow([llm_step("ask_lore", telemetry)], budget_usd=0.0)

    assert workflow.budget.used_usd > 0.0
    assert workflow.budget.remaining_usd < 0.0
    assert workflow.budget.over_budget is True
