"""Tests for IN-6: Step-level latency and cost span on AgentStep.

Covers hard acceptance criteria:
- latency_ms comes from real CallRecord (not hardcoded 0)
- cost_usd comes from real CallRecord (not hardcoded 0)
- Multi-call per step: aggregated correctly
- Planning call NOT included in step span
- records_since(snapshot_idx): correct slice
- records_since out of range raises IndexError

Post-T2 host note: the snapshot/records_since attribution now lives in
ObservationOverflowMiddleware (snapshot before the tool handler, sum after), and planning calls
are recorded by TelemetryCallbackHandler around each model turn — outside every tool window by
construction. The integration tests drive the REAL ReActAgent with a scripted chat model whose
turns carry usage_metadata, and a skill that bills inner CallRecords to the same collector (the
stand-in for an LLM-calling skill).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from owcopilot.agent.react import ReActAgent
from owcopilot.llm.telemetry import CallRecord, TelemetryCollector
from owcopilot.skills import CostTier, SideEffect, Skill, SkillRegistry


class _Bindable(GenericFakeChatModel):
    """Scripted fake accepting ``bind_tools`` (create_agent always binds)."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self


def _scripted(turns: list[AIMessage]) -> _Bindable:
    return _Bindable(messages=iter(turns))


# ---------------------------------------------------------------------------
# Helpers: scripted planner turns (with usage) and a fake LLM-calling skill
# ---------------------------------------------------------------------------


def _plan_turns(n_tool_turns: int = 1, tokens_in: int = 50, tokens_out: int = 20):
    """Script: n tool-calling turns then a final answer, every turn reporting real usage."""
    usage = {
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "total_tokens": tokens_in + tokens_out,
        "input_token_details": {},
    }
    turns = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "check_world", "args": {}, "id": f"call_{i}", "type": "tool_call"}
            ],
            usage_metadata=dict(usage),
        )
        for i in range(n_tool_turns)
    ]
    turns.append(AIMessage(content="all good", usage_metadata=dict(usage)))
    return turns


def _make_registry_with_skill(
    collector: TelemetryCollector | None = None,
    inner_records: list[CallRecord] | None = None,
) -> SkillRegistry:
    """Registry with one deterministic skill that (optionally) bills inner LLM CallRecords to
    *collector* during execution — the stand-in for a skill that calls a model."""

    def _handler(**kwargs: Any) -> dict:
        if collector is not None:
            for record in inner_records or []:
                collector.record(record)
        return {"status": "ok", "issues": []}

    registry = SkillRegistry()
    registry.register(
        Skill(
            name="check_world",
            description="test skill",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=_handler,
        )
    )
    return registry


def _inner(latency_ms: float, *, tokens_in: int = 0, tokens_out: int = 0) -> CallRecord:
    return CallRecord(
        task="skill_llm",
        tier="cheap",
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# records_since: unit tests
# ---------------------------------------------------------------------------


def test_records_since_basic_slice() -> None:
    """records_since returns records after the snapshot index."""
    tc = TelemetryCollector()
    r1 = CallRecord(task="t1", tier="mock", input_tokens=10, output_tokens=5, latency_ms=100.0)
    r2 = CallRecord(task="t2", tier="mock", input_tokens=20, output_tokens=8, latency_ms=200.0)
    r3 = CallRecord(task="t3", tier="mock", input_tokens=30, output_tokens=10, latency_ms=300.0)
    tc.record(r1)
    tc.record(r2)
    tc.record(r3)

    snap = 1
    since = tc.records_since(snap)
    assert len(since) == 2
    assert since[0].task == "t2"
    assert since[1].task == "t3"


def test_records_since_empty_at_end() -> None:
    """Snapshot at current end -> empty list (no new records added)."""
    tc = TelemetryCollector()
    tc.record(CallRecord(task="x", tier="mock", input_tokens=1, output_tokens=1))
    snap = len(tc.records)  # == 1
    since = tc.records_since(snap)
    assert since == []


def test_records_since_zero_returns_all() -> None:
    """Snapshot at 0 -> returns all records."""
    tc = TelemetryCollector()
    for i in range(3):
        tc.record(CallRecord(task=f"t{i}", tier="mock", input_tokens=i, output_tokens=i))
    assert len(tc.records_since(0)) == 3


def test_records_since_out_of_range_raises() -> None:
    """[硬] records_since with index > len(records) raises IndexError."""
    tc = TelemetryCollector()
    with pytest.raises(IndexError):
        tc.records_since(999)


def test_records_since_negative_raises() -> None:
    """[硬] records_since with negative index raises IndexError."""
    tc = TelemetryCollector()
    with pytest.raises(IndexError):
        tc.records_since(-1)


# ---------------------------------------------------------------------------
# AgentStep.latency_ms from CallRecord
# ---------------------------------------------------------------------------


def test_step_latency_ms_from_call_record() -> None:
    """[硬] AgentStep.latency_ms is derived from the real CallRecord, not hardcoded 0.

    The skill bills one inner LLM CallRecord (latency 250 ms) during execution; the step span
    must carry exactly that latency (planning calls stay outside the snapshot window).
    """
    FAKE_LATENCY = 250.0
    collector = TelemetryCollector()
    registry = _make_registry_with_skill(collector, [_inner(FAKE_LATENCY)])
    agent = ReActAgent(
        model=_scripted(_plan_turns()), registry=registry, max_steps=5, telemetry=collector
    )
    result = agent.run("test goal")

    assert len(result.steps) >= 1
    step = result.steps[0]
    assert step.latency_ms == FAKE_LATENCY


def test_step_latency_ms_is_zero_for_deterministic_skill() -> None:
    """[软] Deterministic skill (no LLM calls during execution) -> latency_ms == 0.0."""
    collector = TelemetryCollector()
    registry = _make_registry_with_skill()  # pure deterministic, bills nothing
    agent = ReActAgent(
        model=_scripted(_plan_turns()), registry=registry, max_steps=3, telemetry=collector
    )
    result = agent.run("test goal")

    assert len(result.steps) == 1
    # No LLM calls during skill execution -> step latency should be 0.0 (the planning calls the
    # collector DID record are excluded by the snapshot window).
    assert result.steps[0].latency_ms == 0.0
    assert len(collector.records) >= 2  # planning calls really were recorded alongside


# ---------------------------------------------------------------------------
# AgentStep.cost_usd from CallRecord
# ---------------------------------------------------------------------------


def test_step_cost_usd_from_call_record() -> None:
    """[硬] AgentStep.cost_usd is derived from the real CallRecord (tokens * price)."""
    inner = _inner(100.0, tokens_in=1000, tokens_out=50)  # cheap tier -> non-zero cost
    collector = TelemetryCollector()
    registry = _make_registry_with_skill(collector, [inner])
    agent = ReActAgent(
        model=_scripted(_plan_turns(tokens_in=100, tokens_out=50)),
        registry=registry,
        max_steps=5,
        telemetry=collector,
    )
    result = agent.run("test goal")

    assert len(result.steps) >= 1
    step = result.steps[0]
    assert step.cost_usd == pytest.approx(inner.cost_usd)
    assert step.cost_usd > 0.0  # must come from the real record, not hardcoded


def test_step_cost_usd_zero_for_deterministic_skill() -> None:
    """Deterministic skill with no inner LLM calls -> cost_usd == 0.0."""
    collector = TelemetryCollector()
    registry = _make_registry_with_skill()
    agent = ReActAgent(
        model=_scripted(_plan_turns()), registry=registry, max_steps=3, telemetry=collector
    )
    result = agent.run("test goal")
    assert result.steps[0].cost_usd == 0.0


# ---------------------------------------------------------------------------
# Multi-step: planning call not in step span
# ---------------------------------------------------------------------------


def test_planning_call_not_counted_in_step_span() -> None:
    """[硬] The planning LLM call is NOT counted in the step's latency/cost span.

    The snapshot is taken when the tool call starts, so only records billed DURING skill
    execution are counted — the planning turns (recorded by the callback handler around each
    model call) fall outside every tool window.
    """
    SKILL_LATENCY = 200.0
    collector = TelemetryCollector()
    registry = _make_registry_with_skill(collector, [_inner(SKILL_LATENCY)])
    agent = ReActAgent(
        model=_scripted(_plan_turns(tokens_in=10, tokens_out=5)),
        registry=registry,
        max_steps=5,
        telemetry=collector,
    )
    result = agent.run("test goal")

    assert len(result.steps) >= 1
    step = result.steps[0]
    # Step latency includes ONLY the skill's inner call, never the planning calls.
    assert step.latency_ms == SKILL_LATENCY
    planning_records = [r for r in collector.records if r.task == "agent_react"]
    assert len(planning_records) == 2  # both planning turns were still recorded for the run


# ---------------------------------------------------------------------------
# Multi-call aggregation within a single step
# ---------------------------------------------------------------------------


def test_multi_call_per_step_aggregated() -> None:
    """[硬] Skill that bills multiple inner calls -> step latency/cost aggregated from all."""
    CALL_LATENCY = 100.0
    NUM_SKILL_CALLS = 3
    collector = TelemetryCollector()
    registry = _make_registry_with_skill(
        collector, [_inner(CALL_LATENCY) for _ in range(NUM_SKILL_CALLS)]
    )
    agent = ReActAgent(
        model=_scripted(_plan_turns(tokens_in=20, tokens_out=10)),
        registry=registry,
        max_steps=5,
        telemetry=collector,
    )
    result = agent.run("test goal")

    assert len(result.steps) >= 1
    assert result.steps[0].latency_ms == NUM_SKILL_CALLS * CALL_LATENCY


def test_step_span_exactly_equals_skill_call_records() -> None:
    """[硬] Strong invariant: sum of per-step cost/latency EXACTLY equals the sum over the
    CallRecords billed during skill execution, with the per-iteration planning calls explicitly
    EXCLUDED.

    Why not assert against total_cost: the planning calls (task="agent_react") live in
    total_cost but are, by design, never part of any step span. The construction makes the two
    task labels distinguishable: planning calls carry the agent's own task label (the callback
    handler's default_task), the skill's inner calls use task="skill_llm".
    """
    PLANNING_CALLS = 2  # iteration 1 (action) + iteration 2 (final answer)
    SKILL_CALLS_PER_STEP = 2

    collector = TelemetryCollector()
    inner_records = [
        _inner(37.0, tokens_in=100, tokens_out=50),
        _inner(74.0, tokens_in=100, tokens_out=50),
    ]
    assert len(inner_records) == SKILL_CALLS_PER_STEP
    registry = _make_registry_with_skill(collector, inner_records)
    agent = ReActAgent(
        model=_scripted(_plan_turns(tokens_in=100, tokens_out=50)),
        registry=registry,
        max_steps=5,
        task="agent_react",
        telemetry=collector,
    )
    result = agent.run("test goal")

    planning_records = [r for r in collector.records if r.task == "agent_react"]
    skill_call_records = [r for r in collector.records if r.task != "agent_react"]

    # Sanity: the construction produced the expected split.
    assert len(planning_records) == PLANNING_CALLS
    assert len(skill_call_records) == SKILL_CALLS_PER_STEP

    expected_cost = sum(r.cost_usd for r in skill_call_records)
    expected_latency = sum(r.latency_ms for r in skill_call_records)

    sum_step_cost = sum(s.cost_usd for s in result.steps)
    sum_step_latency = sum(s.latency_ms for s in result.steps)

    # Strong invariant: EXACT equality (abs diff < 1e-10), not a <= bound.
    assert abs(sum_step_cost - expected_cost) < 1e-10
    assert abs(sum_step_latency - expected_latency) < 1e-10

    # Guard against the degenerate all-zero pass: the skill calls must have real, non-zero cost
    # and latency, so the equality above is meaningful rather than 0 == 0.
    assert expected_cost > 0.0
    assert expected_latency > 0.0

    # And the planning calls are genuinely excluded: total_cost strictly exceeds the step sum
    # (the scripted planning turns report real usage, so they bill > $0 on the cheap tier).
    assert collector.total_cost > sum_step_cost
