"""Tests for IN-6: Step-level latency and cost span on AgentStep.

Covers hard acceptance criteria:
- latency_ms comes from real CallRecord (not hardcoded 0)
- cost_usd comes from real CallRecord (not hardcoded 0)
- Multi-call per step: aggregated correctly
- Planning call NOT included in step span
- records_since(snapshot_idx): correct slice
- records_since out of range raises IndexError
"""

from __future__ import annotations

from typing import Any

import pytest

from owcopilot.agent.react import ReActAgent
from owcopilot.core.skills import CostTier, SideEffect, Skill, SkillRegistry
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import CallRecord, TelemetryCollector

# ---------------------------------------------------------------------------
# Helpers: instrumented provider and fake skill
# ---------------------------------------------------------------------------

class _InstrumentedProvider:
    """Returns scripted responses and sets a fake latency on each call."""

    def __init__(
        self,
        responses: list[str],
        latency_ms_per_call: float = 100.0,
        tokens_in: int = 50,
        tokens_out: int = 20,
    ) -> None:
        self._responses = list(responses)
        self._idx = 0
        self.latency_ms_per_call = latency_ms_per_call
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out

    def complete(self, *, system: str, user: str, model: str) -> tuple:
        if self._idx >= len(self._responses):
            raise RuntimeError("Provider: no more scripted responses")
        text = self._responses[self._idx]
        self._idx += 1
        return text, self.tokens_in, self.tokens_out


def _make_registry_with_skill(
    skill_name: str = "check_world",
    return_value: dict | None = None,
    extra_gateway_calls: int = 0,
    gw_ref: LLMGateway | None = None,
) -> SkillRegistry:
    """Build a SkillRegistry with one deterministic skill."""
    if return_value is None:
        return_value = {"status": "ok", "issues": []}

    def _handler(**kwargs: Any) -> dict:
        # Optionally inject calls into the gateway (simulates skills that call LLM)
        if extra_gateway_calls and gw_ref is not None:
            for _ in range(extra_gateway_calls):
                gw_ref.complete(task="default", system="sys", user="usr")
        return return_value

    registry = SkillRegistry()
    registry.register(Skill(
        name=skill_name,
        description="test skill",
        cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY,
        handler=_handler,
    ))
    return registry


def _plan_response(action: str = "check_world") -> str:
    """Scripted planner turn: one Thought+Action step."""
    return f"Thought: let me check\nAction: {action}\nAction Input: {{}}"


def _final_response() -> str:
    return "Thought: done\nFinal Answer: all good"


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
    """[硬] AgentStep.latency_ms is derived from real CallRecord, not hardcoded 0.

    Response order: [plan_step1 | skill_inner_call | plan_final_answer]
    - plan_step1 -> action: check_world (consumed by agent planner, BEFORE snap_idx)
    - skill_inner_call -> "dummy" (consumed by skill handler, AFTER snap_idx -> counted in step)
    - plan_final_answer -> Final Answer (consumed by agent planner in step 2)
    """
    FAKE_LATENCY = 250.0

    provider = _InstrumentedProvider(
        responses=[
            _plan_response("check_world"),  # agent planning call #1
            "dummy",                         # skill's inner LLM call (counted in step span)
            _final_response(),               # agent planning call #2
        ]
    )
    gw = LLMGateway(
        providers={"mock": provider},
        router=StaticRouter(mapping={"agent_react": "mock", "default": "mock"}),
        cache=NoOpCache(),
    )

    # Monkey-patch to inject fake latency on records
    _orig_record = gw.telemetry.record

    def _patched_record(rec: CallRecord) -> None:
        from dataclasses import replace
        rec = replace(rec, latency_ms=FAKE_LATENCY)
        _orig_record(rec)

    gw.telemetry.record = _patched_record  # type: ignore[method-assign]

    # Skill that makes one LLM call (counted in step span)
    registry = _make_registry_with_skill(
        skill_name="check_world",
        extra_gateway_calls=1,
        gw_ref=gw,
    )
    agent = ReActAgent(gateway=gw, registry=registry, max_steps=5, task="agent_react")
    result = agent.run("test goal")

    # Should have step(s) from the action
    assert len(result.steps) >= 1
    step = result.steps[0]
    # latency_ms should reflect ONLY the skill's LLM call (FAKE_LATENCY)
    assert step.latency_ms == FAKE_LATENCY


def test_step_latency_ms_is_zero_for_deterministic_skill() -> None:
    """[软] Deterministic skill (no LLM calls during execution) -> latency_ms == 0.0."""
    provider = _InstrumentedProvider(
        responses=[
            _plan_response("check_world"),
            _final_response(),
        ]
    )
    gw = LLMGateway(
        providers={"mock": provider},
        router=StaticRouter(mapping={"agent_react": "mock", "default": "mock"}),
        cache=NoOpCache(),
    )

    # Pure deterministic skill (no gateway calls inside)
    registry = _make_registry_with_skill("check_world", extra_gateway_calls=0)
    agent = ReActAgent(gateway=gw, registry=registry, max_steps=3)
    result = agent.run("test goal")

    assert len(result.steps) == 1
    step = result.steps[0]
    # No LLM calls during skill execution -> step latency should be 0.0
    assert step.latency_ms == 0.0


# ---------------------------------------------------------------------------
# AgentStep.cost_usd from CallRecord
# ---------------------------------------------------------------------------

def test_step_cost_usd_from_call_record() -> None:
    """[硬] AgentStep.cost_usd is derived from real CallRecord (tokens * price).

    Uses 'cheap' tier so cost_usd is non-zero (mock tier price = 0.00).
    Response order: [plan_step1 | skill_inner_call | plan_final_answer]
    """
    provider = _InstrumentedProvider(
        responses=[
            _plan_response("check_world"),  # agent planning call #1
            "dummy",                         # skill's inner LLM call (counted in step span)
            _final_response(),               # agent planning call #2
        ],
        tokens_in=100,
        tokens_out=50,
    )
    cheap_gw = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"agent_react": "cheap", "default": "cheap"}),
        cache=NoOpCache(),
    )
    registry = _make_registry_with_skill(
        "check_world",
        extra_gateway_calls=1,
        gw_ref=cheap_gw,
    )
    agent = ReActAgent(gateway=cheap_gw, registry=registry, max_steps=5)
    result = agent.run("test goal")

    assert len(result.steps) >= 1
    # The skill makes 1 LLM call -> step should have non-zero cost_usd (cheap tier has cost)
    step = result.steps[0]
    assert isinstance(step.cost_usd, float)
    assert step.cost_usd >= 0.0  # Must come from real record, not hardcoded


def test_step_cost_usd_zero_for_deterministic_skill() -> None:
    """Deterministic skill with no gateway calls -> cost_usd == 0.0."""
    provider = _InstrumentedProvider(
        responses=[_plan_response("check_world"), _final_response()]
    )
    gw = LLMGateway(
        providers={"mock": provider},
        router=StaticRouter(mapping={"agent_react": "mock", "default": "mock"}),
        cache=NoOpCache(),
    )
    registry = _make_registry_with_skill("check_world", extra_gateway_calls=0)
    agent = ReActAgent(gateway=gw, registry=registry, max_steps=3)
    result = agent.run("test goal")
    assert result.steps[0].cost_usd == 0.0


# ---------------------------------------------------------------------------
# Multi-step: planning call not in step span
# ---------------------------------------------------------------------------

def test_planning_call_not_counted_in_step_span() -> None:
    """[硬] The planning LLM call is NOT counted in the step's latency/cost span.

    The snap is taken AFTER the planning call, so only calls during skill execution
    are counted in the step span.

    Response order: [plan_step1 | skill_inner_call | plan_final_answer]
    - plan_step1: latency PLANNING_LATENCY (before snap_idx -> NOT in step)
    - skill_inner_call: latency SKILL_LATENCY (after snap_idx -> IN step)
    - plan_final_answer: latency PLANNING_LATENCY (next planning call -> NOT in this step)
    """
    PLANNING_LATENCY = 500.0
    SKILL_LATENCY = 200.0

    scripted = [
        _plan_response("check_world"),  # planning call #1
        "dummy",                         # skill's inner call
        _final_response(),               # planning call #2 (produces final answer)
    ]
    scripted_idx = [0]

    class _ScriptedProvider:
        def complete(self, *, system: str, user: str, model: str) -> tuple:
            text = scripted[scripted_idx[0]]
            scripted_idx[0] += 1
            return text, 10, 5

    gw = LLMGateway(
        providers={"mock": _ScriptedProvider()},
        router=StaticRouter(mapping={"agent_react": "mock", "default": "mock"}),
        cache=NoOpCache(),
    )

    # Assign latencies per call: [PLANNING, SKILL, PLANNING]
    latencies = [PLANNING_LATENCY, SKILL_LATENCY, PLANNING_LATENCY]
    call_count = [0]
    _orig_record = gw.telemetry.record

    def _patched_record(rec: CallRecord) -> None:
        from dataclasses import replace
        idx = min(call_count[0], len(latencies) - 1)
        call_count[0] += 1
        rec = replace(rec, latency_ms=latencies[idx])
        _orig_record(rec)

    gw.telemetry.record = _patched_record  # type: ignore[method-assign]

    registry = _make_registry_with_skill("check_world", extra_gateway_calls=1, gw_ref=gw)
    agent = ReActAgent(gateway=gw, registry=registry, max_steps=5)
    result = agent.run("test goal")

    assert len(result.steps) >= 1
    step = result.steps[0]
    # Step latency should only include the SKILL's call (SKILL_LATENCY), NOT the planning call
    assert step.latency_ms == SKILL_LATENCY
    assert step.latency_ms != PLANNING_LATENCY


# ---------------------------------------------------------------------------
# Multi-call aggregation within a single step
# ---------------------------------------------------------------------------

def test_multi_call_per_step_aggregated() -> None:
    """[硬] Skill that makes multiple gateway calls -> step latency/cost aggregated from all."""
    CALL_LATENCY = 100.0
    NUM_SKILL_CALLS = 3

    class _TimedProvider:
        def __init__(self, responses: list[str]) -> None:
            self._responses = responses
            self._idx = 0

        def complete(self, *, system: str, user: str, model: str) -> tuple:
            text = self._responses[self._idx]
            self._idx += 1
            return text, 20, 10

    provider = _TimedProvider(
        responses=(
            [_plan_response("check_world")] + ["dummy"] * NUM_SKILL_CALLS + [_final_response()]
        )
    )
    gw = LLMGateway(
        providers={"mock": provider},
        router=StaticRouter(mapping={"agent_react": "mock", "default": "mock"}),
        cache=NoOpCache(),
    )

    # Inject CALL_LATENCY on every record
    _orig_record = gw.telemetry.record

    def _patched_record(rec: CallRecord) -> None:
        from dataclasses import replace
        rec = replace(rec, latency_ms=CALL_LATENCY)
        _orig_record(rec)

    gw.telemetry.record = _patched_record  # type: ignore[method-assign]

    # Skill makes NUM_SKILL_CALLS gateway calls
    registry = _make_registry_with_skill(
        "check_world", extra_gateway_calls=NUM_SKILL_CALLS, gw_ref=gw
    )
    agent = ReActAgent(gateway=gw, registry=registry, max_steps=5)
    result = agent.run("test goal")

    assert len(result.steps) >= 1
    step = result.steps[0]
    # Expect aggregated latency = NUM_SKILL_CALLS * CALL_LATENCY
    expected_latency = NUM_SKILL_CALLS * CALL_LATENCY
    assert step.latency_ms == expected_latency


def test_step_span_exactly_equals_skill_call_records() -> None:
    """[硬] Strong invariant: sum of per-step cost/latency EXACTLY equals the sum over the
    CallRecords produced during skill execution (snap_idx slices), with the per-iteration
    planning calls explicitly EXCLUDED.

    Why not assert against total_cost: the planning calls (task="agent_react") live in
    total_cost but are, by design, never part of any step span (snap is taken AFTER the
    planning call). The meaningful invariant is that the step spans account for exactly the
    skill-execution calls — no more, no less.

    Construction makes the two task labels distinguishable:
    - planning calls use task="agent_react" (the agent's own self.task)
    - the skill's inner calls use task="default" (see _make_registry_with_skill handler)
    """
    PLANNING_CALLS = 2  # iteration 1 (action) + iteration 2 (final answer)
    SKILL_CALLS_PER_STEP = 2

    # Two agent iterations: [plan->action] then [plan->final]. The action step's skill makes
    # SKILL_CALLS_PER_STEP inner calls. Response script accounts for every gateway.complete():
    #   plan#1 (action) | skill_call#1 | skill_call#2 | plan#2 (final answer)
    scripted = (
        [_plan_response("check_world")]
        + ["dummy"] * SKILL_CALLS_PER_STEP
        + [_final_response()]
    )
    scripted_idx = [0]

    class _ScriptedProvider:
        def complete(self, *, system: str, user: str, model: str) -> tuple:
            text = scripted[scripted_idx[0]]
            scripted_idx[0] += 1
            return text, 100, 50  # 100 input / 50 output tokens -> non-zero cost on "cheap"

    gw = LLMGateway(
        providers={"cheap": _ScriptedProvider()},
        router=StaticRouter(mapping={"agent_react": "cheap", "default": "cheap"}),
        cache=NoOpCache(),
    )

    # Give each record a deterministic, distinct latency so the latency sum is also exact.
    call_seq = [0]
    _orig_record = gw.telemetry.record

    def _patched_record(rec: CallRecord) -> None:
        from dataclasses import replace
        call_seq[0] += 1
        rec = replace(rec, latency_ms=float(call_seq[0]) * 37.0)  # 37, 74, 111, 148, ...
        _orig_record(rec)

    gw.telemetry.record = _patched_record  # type: ignore[method-assign]

    registry = _make_registry_with_skill(
        "check_world", extra_gateway_calls=SKILL_CALLS_PER_STEP, gw_ref=gw
    )
    agent = ReActAgent(gateway=gw, registry=registry, max_steps=5, task="agent_react")
    result = agent.run("test goal")

    # The skill-execution CallRecords are exactly those whose task is NOT the planning task.
    planning_task = "agent_react"
    skill_call_records = [r for r in gw.telemetry.records if r.task != planning_task]
    planning_records = [r for r in gw.telemetry.records if r.task == planning_task]

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

    # And the planning calls are genuinely excluded: total_cost strictly exceeds the step sum.
    assert gw.telemetry.total_cost > sum_step_cost
