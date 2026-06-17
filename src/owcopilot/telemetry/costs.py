"""Cost-budget summaries across deterministic and LLM workflow steps."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..llm.telemetry import TelemetryCollector


class StepKind(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"


class StepTelemetry(BaseModel):
    name: str
    kind: StepKind
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    # This step's cost is a ballpark (a tier it used isn't priced via env). Deterministic ($0) steps
    # are always exact. Propagated up so the workflow flag reflects the tiers actually used.
    cost_is_estimate: bool = False


class CostBudgetSummary(BaseModel):
    budget_usd: float | None = Field(default=None, ge=0)
    used_usd: float = 0.0
    remaining_usd: float | None = None
    over_budget: bool = False
    # True when an LLM ran but no real prices are configured — the figure is a ballpark estimate
    # from illustrative tier prices, not actual vendor billing. Deterministic ($0) steps are exact.
    cost_is_estimate: bool = False


class WorkflowTelemetry(BaseModel):
    steps: list[StepTelemetry]
    budget: CostBudgetSummary


def deterministic_step(name: str) -> StepTelemetry:
    return StepTelemetry(name=name, kind=StepKind.DETERMINISTIC)


def llm_step(name: str, telemetry: TelemetryCollector | Mapping[str, Any]) -> StepTelemetry:
    summary = telemetry.summary() if isinstance(telemetry, TelemetryCollector) else telemetry
    return StepTelemetry(
        name=name,
        kind=StepKind.LLM,
        calls=int(summary.get("calls", 0)),
        input_tokens=int(summary.get("input_tokens", 0)),
        output_tokens=int(summary.get("output_tokens", 0)),
        total_cost_usd=float(summary.get("total_cost_usd", 0.0)),
        cost_is_estimate=bool(summary.get("cost_is_estimate", False)),
    )


def summarize_workflow(
    steps: list[StepTelemetry],
    *,
    budget_usd: float | None = None,
) -> WorkflowTelemetry:
    used = round(sum(step.total_cost_usd for step in steps), 6)
    remaining = None if budget_usd is None else round(budget_usd - used, 6)
    return WorkflowTelemetry(
        steps=steps,
        budget=CostBudgetSummary(
            budget_usd=budget_usd,
            used_usd=used,
            remaining_usd=remaining,
            over_budget=(budget_usd is not None and used > budget_usd),
            # A ballpark if any step priced a real call from illustrative defaults (a tier it used
            # isn't configured); deterministic steps are always exact, so they never set this.
            cost_is_estimate=any(step.cost_is_estimate for step in steps),
        ),
    )
