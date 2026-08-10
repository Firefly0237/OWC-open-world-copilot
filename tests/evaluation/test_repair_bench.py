"""MCTS repair planner vs greedy baseline benchmark."""

from __future__ import annotations

from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.evaluation.repair_bench import (
    _independent_world,
    _trap_candidate_provider,
    _trap_world,
    compare_planners,
    greedy_repair,
    run_repair_benchmark,
)


def _runner() -> AuditRunner:
    return AuditRunner(build_default_rule_registry())


def test_greedy_clears_all_independent_errors() -> None:
    plan = greedy_repair(_independent_world(3), _runner())
    assert plan.initial_open_errors == 6  # 3 quests x (dangling giver + missing localization)
    assert plan.final_open_errors == 0


def test_mcts_matches_greedy_on_independent_fixes() -> None:
    row = compare_planners("independent-3", _independent_world(3), _runner())
    assert row.greedy_resolved == row.mcts_resolved == row.initial_errors
    assert row.greedy_plan_len == row.mcts_plan_len
    assert not row.mcts_beats_greedy  # independent fixes: both reach the optimum -> tie


def test_mcts_beats_greedy_when_fixes_interact() -> None:
    row = compare_planners(
        "trap", _trap_world(), _runner(), candidate_provider=_trap_candidate_provider
    )
    assert row.initial_errors == 3
    assert row.greedy_resolved == 2  # greedy grabs the broad fix and strands q3
    assert row.mcts_resolved == 3  # lookahead takes the narrow path and resolves all three
    assert row.mcts_beats_greedy


def test_benchmark_invariant_mcts_never_regresses_below_greedy() -> None:
    rows = run_repair_benchmark()
    assert len(rows) == 3
    assert all(row.mcts_at_least_greedy for row in rows)  # the invariant
    assert any(row.mcts_beats_greedy for row in rows)  # and it strictly wins on the trap
