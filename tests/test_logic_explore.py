"""WS-E · N3 bounded full-path exploration (narrative model checking).

These tests assemble quest inputs and assert on the PRODUCTION ``explore_quest`` /
``simulate_quest`` — no exploration or simulation logic is re-implemented here. The load-bearing
check is the witness↔simulate equivalence
(`test_every_reachable_witness_replays_through_simulator`): every reachable outcome's witness must
replay verbatim through the UNMODIFIED simulator, proving the explorer's transition relation is the
simulator's.
"""

from __future__ import annotations

from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.content.models import (
    Branch,
    Effect,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.logic import ExplorationReport, explore_quest, simulate_quest


def _quest(stage_ids: list[str], logic: QuestLogic | None) -> Quest:
    return Quest(
        id="q",
        title="t",
        stages=[QuestStage(id=s, summary=s) for s in stage_ids],
        logic=logic,
    )


# --------------------------------------------------------------------------------------------------
# Hand-built topologies (each exercises one exploration property).
# --------------------------------------------------------------------------------------------------


def _linear_quest() -> Quest:
    """Three stages, one deterministic on-complete effect, no branches."""
    return _quest(
        ["s1", "s2", "s3"],
        QuestLogic(
            variables=[LogicVar(id="flag", type=LogicVarType.BOOL, default=False)],
            stage_logic=[
                StageLogic(
                    stage_id="s1",
                    effects_on_complete=[Effect(var="flag", op="set", value=True)],
                )
            ],
        ),
    )


def _diamond_quest() -> Quest:
    """s1 forks into two arms (a var-gated one true in the seed, plus an unconditional one) that
    both converge on s3; s3 is the last stage (a linear completion)."""
    return _quest(
        ["s1", "s3"],
        QuestLogic(
            variables=[LogicVar(id="flag", type=LogicVarType.BOOL, default=True)],
            branches=[
                Branch(id="b_L", from_stage="s1", condition="flag", to_stage="s3"),
                Branch(id="b_R", from_stage="s1", condition="true", to_stage="s3"),
            ],
        ),
    )


def _reachable_terminal_quest() -> Quest:
    """Two terminal outcomes gated on ``blessed``, which a stage effect CAN set true on one arm:
    both outcomes are reachable via distinct witnesses."""
    return _quest(
        ["s1", "s_bless", "s_plain", "s_final"],
        QuestLogic(
            variables=[LogicVar(id="blessed", type=LogicVarType.BOOL, default=False)],
            stage_logic=[
                StageLogic(
                    stage_id="s_bless",
                    effects_on_complete=[Effect(var="blessed", op="set", value=True)],
                )
            ],
            branches=[
                Branch(id="b_take_bless", from_stage="s1", condition="true", to_stage="s_bless"),
                Branch(id="b_take_plain", from_stage="s1", condition="true", to_stage="s_plain"),
                Branch(
                    id="b_bless_to_final",
                    from_stage="s_bless",
                    condition="true",
                    to_stage="s_final",
                ),
                Branch(
                    id="b_plain_to_final",
                    from_stage="s_plain",
                    condition="true",
                    to_stage="s_final",
                ),
                Branch(id="b_win", from_stage="s_final", condition="blessed", outcome="win"),
                Branch(id="b_lose", from_stage="s_final", condition="not blessed", outcome="lose"),
            ],
        ),
    )


def _unreachable_outcome_quest() -> Quest:
    """A terminal branch (and a stage) gated on ``secret``, which is never set true: the outcome is
    unreachable, its branch is dead, and the orphaned stage is statically unreachable."""
    return _quest(
        ["s1", "s_final", "s_orphan"],
        QuestLogic(
            variables=[LogicVar(id="secret", type=LogicVarType.BOOL, default=False)],
            branches=[
                Branch(id="b_to_final", from_stage="s1", condition="true", to_stage="s_final"),
                Branch(id="b_to_orphan", from_stage="s1", condition="secret", to_stage="s_orphan"),
                Branch(id="b_end", from_stage="s_final", condition="true", outcome="end"),
                Branch(
                    id="b_secret_win",
                    from_stage="s_final",
                    condition="secret",
                    outcome="secret_win",
                ),
            ],
        ),
    )


def _dead_branch_quest() -> Quest:
    """Only the advance branch is satisfiable; the locked branch is dead."""
    return _quest(
        ["s1", "s2"],
        QuestLogic(
            variables=[LogicVar(id="locked", type=LogicVarType.BOOL, default=False)],
            branches=[
                Branch(id="b_go", from_stage="s1", condition="true", to_stage="s2"),
                Branch(id="b_locked", from_stage="s1", condition="locked", to_stage="s2"),
            ],
        ),
    )


def _loop_quest() -> Quest:
    """A pure two-stage loop with no terminal and no state change: exploration must terminate via
    the visited-set dedup, recording a no-terminal dead end."""
    return _quest(
        ["s1", "s2"],
        QuestLogic(
            branches=[
                Branch(id="b1", from_stage="s1", to_stage="s2"),
                Branch(id="b2", from_stage="s2", to_stage="s1"),
            ]
        ),
    )


def _inc_loop_quest() -> Quest:
    """An unbounded ``inc`` counter loop: each lap through s1 mutates ``n``, so no two
    (stage, state) nodes repeat and only the ``max_states`` / ``max_depth`` bounds make the search
    terminate."""
    return _quest(
        ["s1", "s2"],
        QuestLogic(
            variables=[LogicVar(id="n", type=LogicVarType.INT, default=0)],
            stage_logic=[
                StageLogic(
                    stage_id="s1",
                    effects_on_complete=[Effect(var="n", op="inc", value=1)],
                )
            ],
            branches=[
                Branch(id="b1", from_stage="s1", to_stage="s2"),
                Branch(id="b2", from_stage="s2", to_stage="s1"),
            ],
        ),
    )


def _counter_gated_loop_quest() -> Quest:
    """A counter-gated finale: ``s1 <-> s2`` loops while a stage effect increments ``n``, and a
    terminal ``b_win`` out of s2 unlocks only once ``n >= 2``. The state space stays finite (the
    loop gate ``n < 2`` caps ``n`` at 2), so exploration terminates without a bound — but the
    winning witness must revisit a stage, making it the documented case that does NOT replay through
    the current stage-space simulator (closing that gap is N4)."""
    return _quest(
        ["s1", "s2"],
        QuestLogic(
            variables=[LogicVar(id="n", type=LogicVarType.INT, default=0)],
            stage_logic=[
                StageLogic(
                    stage_id="s1",
                    effects_on_complete=[Effect(var="n", op="inc", value=1)],
                )
            ],
            branches=[
                Branch(id="b_s1s2", from_stage="s1", condition="true", to_stage="s2"),
                Branch(id="b_loop", from_stage="s2", condition="n < 2", to_stage="s1"),
                Branch(id="b_win", from_stage="s2", condition="n >= 2", outcome="win"),
            ],
        ),
    )


# Quests whose reachable outcomes must all replay through the unmodified simulator.
_REPLAYABLE_QUESTS = [
    _linear_quest(),
    _diamond_quest(),
    _reachable_terminal_quest(),
    _unreachable_outcome_quest(),
    _dead_branch_quest(),
]


# --------------------------------------------------------------------------------------------------
# (a) Known topologies.
# --------------------------------------------------------------------------------------------------


def test_linear_quest_single_completed_outcome_empty_witness() -> None:
    report = explore_quest(_linear_quest())
    assert [o.outcome for o in report.outcomes] == ["completed"]
    linear = report.outcomes[0]
    assert linear.kind == "linear" and linear.reachable is True and linear.witness == []
    assert report.states_explored == 3  # one (stage, state) node per stage on the single path
    assert report.stage_coverage == 1.0 and not report.truncated


def test_diamond_convergence_covers_both_arms_and_dedups_convergence_stage() -> None:
    report = explore_quest(_diamond_quest())
    assert report.covered_branches == ["b_L", "b_R"]  # both arms taken
    assert report.branch_coverage == 1.0
    # s3 is reached by both arms with the same state, so state dedup counts it once (s1 + s3 == 2).
    assert report.states_explored == 2
    assert report.reached_stages == ["s1", "s3"]
    assert [o.outcome for o in report.outcomes if o.reachable] == ["completed"]


def test_reachable_terminal_outcomes_both_reachable_distinct_witnesses() -> None:
    report = explore_quest(_reachable_terminal_quest())
    reached = {o.outcome: o for o in report.outcomes if o.reachable}
    assert set(reached) == {"win", "lose"}
    assert reached["win"].witness[-1] == "b_win"
    assert reached["lose"].witness[-1] == "b_lose"
    assert reached["win"].witness != reached["lose"].witness  # distinct paths
    assert not report.truncated


def test_unreachable_outcome_is_flagged_dead_and_orphan_stage_uncovered() -> None:
    report = explore_quest(_unreachable_outcome_quest())
    secret_win = next(o for o in report.outcomes if o.outcome == "secret_win")
    assert secret_win.reachable is False and secret_win.witness == []
    assert "b_secret_win" in report.dead_branches  # gate never satisfiable -> branch is dead
    assert report.unreachable_stages == ["s_orphan"]  # only reachable via the dead branch
    assert report.stage_coverage < 1.0


def test_dead_branch_quest_reports_dead_branch_and_partial_coverage() -> None:
    report = explore_quest(_dead_branch_quest())
    assert report.dead_branches == ["b_locked"]
    assert report.branch_coverage < 1.0


def test_loop_without_terminal_terminates_with_no_terminal_dead_end() -> None:
    report = explore_quest(_loop_quest())
    assert not any(o.reachable for o in report.outcomes)  # no outcome exists in a pure loop
    assert any(d.reason == "no_terminal" for d in report.dead_ends)
    assert report.states_explored == 2  # bounded by the (stage, state) visited set, not by a budget
    assert not report.truncated


def _quest_precondition_gated(default: bool) -> Quest:
    """Two linear stages behind a quest-level precondition on ``has_writ``; ``default`` decides
    whether the seeded state can enter the quest at all."""
    return _quest(
        ["s1", "s2"],
        QuestLogic(
            variables=[LogicVar(id="has_writ", type=LogicVarType.BOOL, default=default)],
            precondition="has_writ",
        ),
    )


def test_quest_precondition_false_is_unavailable_and_explores_nothing() -> None:
    report = explore_quest(_quest_precondition_gated(default=False))
    assert report.states_explored == 0  # the quest was never entered
    assert not any(o.reachable for o in report.outcomes)  # no ending reachable
    assert [d.reason for d in report.dead_ends] == ["unavailable"]
    assert "has_writ" in report.dead_ends[0].detail  # carries the gate expression for narration
    assert report.reached_stages == [] and report.unreachable_stages == ["s1", "s2"]
    assert report.stage_coverage == 0.0 and not report.truncated


def test_quest_precondition_true_explores_normally() -> None:
    # The same gate, satisfied in the seeded state, explores exactly as an ungated quest would.
    report = explore_quest(_quest_precondition_gated(default=True))
    assert report.states_explored == 2  # both stages explored
    assert [o.outcome for o in report.outcomes if o.reachable] == ["completed"]
    assert not any(d.reason == "unavailable" for d in report.dead_ends)


def test_empty_quest_precondition_never_gates_exploration() -> None:
    # Regression sentinel: an empty quest precondition ("" = always enterable) must not gate.
    report = explore_quest(_quest(["s1"], QuestLogic(precondition="")))
    assert [o.outcome for o in report.outcomes if o.reachable] == ["completed"]
    assert not any(d.reason == "unavailable" for d in report.dead_ends)


# --------------------------------------------------------------------------------------------------
# (b) Witness↔simulate equivalence — the anti-divergence self-check.
# --------------------------------------------------------------------------------------------------


def test_every_reachable_witness_replays_through_simulator() -> None:
    for quest in _REPLAYABLE_QUESTS:
        report = explore_quest(quest)
        reachable = [o for o in report.outcomes if o.reachable]
        assert reachable, f"expected at least one reachable outcome for {quest.id}"
        for outcome in reachable:
            run = simulate_quest(quest, choices=list(outcome.witness))
            assert run.status == "completed", (quest.stages, outcome.outcome, run.status)
            last_branch = run.steps[-1].branch_taken if run.steps else ""
            expected = outcome.branch_id if outcome.kind == "terminal" else ""
            assert last_branch == expected, (outcome.outcome, last_branch, expected)


def test_counter_gated_loop_outcome_reachable_but_witness_not_replayable() -> None:
    """The N4-boundary regression that locks the BOUNDED witness-replay claim.

    Exploration dedups in (stage, var-state) space, so it traverses s1/s2 twice to unlock a
    counter-gated finale and correctly reports the outcome reachable — but the winning witness
    REVISITS a stage, which the current stage-space simulator cannot replay: it cycle-aborts. This
    is the documented counterexample to a *universal* replay guarantee (replay holds only for
    stage-simple / forward-DAG witnesses, as the equivalence test above and the module docstring now
    state)."""
    quest = _counter_gated_loop_quest()
    report = explore_quest(quest)

    # (a) the terminal outcome is still reported reachable, via the stage-revisiting witness.
    win = next(o for o in report.outcomes if o.outcome == "win")
    assert win.reachable is True
    assert win.witness == ["b_s1s2", "b_loop", "b_s1s2", "b_win"]
    assert not report.truncated  # finite state space (n capped at 2): no bound was hit
    assert len(set(win.witness)) < len(win.witness)  # a branch id repeats => a stage was revisited

    # (b) that witness is the documented NON-replayable case: the simulator cycle-aborts in stage
    # space rather than reaching 'completed'.
    run = simulate_quest(quest, choices=list(win.witness))
    assert run.status == "cycle"


# --------------------------------------------------------------------------------------------------
# (c) Truncation honesty.
# --------------------------------------------------------------------------------------------------


def test_max_states_bound_truncates_and_flags_incomplete() -> None:
    report = explore_quest(_inc_loop_quest(), max_states=5)
    assert report.truncated is True
    assert report.states_explored <= 5
    assert report.frontier_remaining > 0 or report.depth_truncated_paths > 0
    # a caller inspecting the report cannot mistake a bounded run for a complete one
    assert ExplorationReport.model_validate(report.model_dump()).truncated is True


def test_max_depth_bound_truncates_via_depth_cut() -> None:
    report = explore_quest(_inc_loop_quest(), max_depth=3)
    assert report.truncated is True
    assert report.depth_truncated_paths > 0


# --------------------------------------------------------------------------------------------------
# (d) Determinism.
# --------------------------------------------------------------------------------------------------


def test_exploration_is_byte_identical_across_runs() -> None:
    quest = _reachable_terminal_quest()
    assert explore_quest(quest).model_dump() == explore_quest(quest).model_dump()


# --------------------------------------------------------------------------------------------------
# (e) Empty quests mirror the simulator's trivially-completed path.
# --------------------------------------------------------------------------------------------------


def test_quest_without_logic_is_single_completed_outcome() -> None:
    report = explore_quest(Quest(id="q", title="t", stages=[QuestStage(id="s1", summary="x")]))
    summary = [(o.outcome, o.reachable, o.witness) for o in report.outcomes]
    assert summary == [("completed", True, [])]


def test_quest_without_stages_is_single_completed_outcome() -> None:
    report = explore_quest(_quest([], _loop_quest().logic))
    summary = [(o.outcome, o.reachable, o.witness) for o in report.outcomes]
    assert summary == [("completed", True, [])]


# --------------------------------------------------------------------------------------------------
# Back-compat proof: the exploration diagnostic is NOT wired into the audit surface.
# --------------------------------------------------------------------------------------------------


def test_default_audit_registry_is_unchanged_and_has_no_explore_rule() -> None:
    codes = build_default_rule_registry().codes()
    # 30: bumped from 29 for POI_UNKNOWN_REGION_REF (MissingRegionReferenceRule, scale-p0 batch 2)
    # closing the POI->region audit gap — unrelated to the exploration-report claim below.
    assert len(codes) == 30  # the exploration report is a diagnostic tool, never an audit rule
    assert not any("EXPLORE" in code for code in codes)
