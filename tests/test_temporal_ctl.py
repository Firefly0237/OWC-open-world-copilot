# 切片式 CTL 时序检查引擎的专属测试：软锁/不变式/可达 + 切片压缩 + 反发散 + 评测世界实跑。
"""Sliced-CTL temporal engine (``owcopilot.logic.temporal``) tests.

Every test assembles inputs and asserts on the production functions — the CTL checks, the slicer,
and (for the anti-divergence guard) the unmodified ``explore_quest`` / ``simulate_quest`` — never
re-implementing any transition or checking logic here.
"""

from __future__ import annotations

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
from owcopilot.evaluation.acceptance import build_acceptance_world
from owcopilot.evaluation.complexity_corpus import (
    generate_complexity_bundle,
    load_complexity_profiles,
)
from owcopilot.logic.explore import explore_quest
from owcopilot.logic.simulate import simulate_quest
from owcopilot.logic.temporal import (
    PropertyPattern,
    SpecStatus,
    TemporalSpec,
    check_soft_lock,
    check_temporal_spec,
    cone_of_influence,
    parse_specs_from_quest,
)


def _quest(quest_id: str, stages: list[str], logic: QuestLogic) -> Quest:
    return Quest(
        id=quest_id,
        title=quest_id,
        stages=[QuestStage(id=s, summary="") for s in stages],
        logic=logic,
    )


def _softlock_quest() -> Quest:
    """s0 forks: b_good reaches an ending; b_trap latches ``stuck`` and lands in s2 whose only exit
    needs ``not stuck`` → a reachable state that can never reach any ending."""
    return _quest(
        "q_softlock",
        ["s0", "s1", "s2"],
        QuestLogic(
            variables=[LogicVar(id="stuck", type=LogicVarType.BOOL, default=False)],
            branches=[
                Branch(id="b_good", from_stage="s0", to_stage="s1"),
                Branch(
                    id="b_trap", from_stage="s0", to_stage="s2",
                    effects=[Effect(var="stuck", op="set", value=True)],
                ),
                Branch(id="b_win", from_stage="s1", outcome="win"),
                Branch(id="b_exit", from_stage="s2", condition="not stuck", outcome="escaped"),
            ],
        ),
    )


# --------------------------------------------------------------------------------------------------
# AG EF — soft-lock (the flagship, CTL-unique).
# --------------------------------------------------------------------------------------------------


def test_soft_lock_detected_with_replayable_witness() -> None:
    result = check_soft_lock(_softlock_quest())
    assert result.status is SpecStatus.VIOLATED
    assert result.holds is False
    assert result.strong is True  # exhaustive → the soft-lock verdict is sound
    assert result.counterexample_witness == ["b_trap"]
    assert result.counterexample_stage == "s2"


def test_soft_lock_witness_replays_through_the_unmodified_simulator() -> None:
    quest = _softlock_quest()
    result = check_soft_lock(quest)
    run = simulate_quest(quest, choices=result.counterexample_witness)
    # The witness drives the player into the dead state; the simulator gets stuck there (deadlock),
    # confirming the counterexample is a genuine reproducible path, not an artifact.
    assert result.counterexample_stage in run.path
    assert run.status == "deadlock"


def test_soft_lock_withheld_when_dead_end_depends_on_an_external_flag() -> None:
    """A dead-end blocked by a flag the quest never writes (a dialogue-set / cross-quest flag) is
    NOT a sound single-quest soft-lock — the owning layer may clear it. The verdict is WITHHELD, not
    a false-positive VIOLATED (the fix for the demo's ``quest_gate``: ``s_act`` needs ``helped``,
    which only the dialogue sets)."""
    quest = _quest(
        "quest_gate",
        ["s_talk", "s_act"],
        QuestLogic(
            variables=[
                LogicVar(id="helped", type=LogicVarType.BOOL, default=False),
                LogicVar(id="rep", type=LogicVarType.INT, default=0),
            ],
            stage_logic=[StageLogic(stage_id="s_act", precondition="helped")],
            branches=[
                Branch(id="b_peace", from_stage="s_act", condition="helped", outcome="peaceful"),
                Branch(
                    id="b_force", from_stage="s_act", condition="not helped", outcome="forceful"
                ),
            ],
        ),
    )
    result = check_soft_lock(quest)
    assert result.status is SpecStatus.WITHHELD
    assert result.holds is True and result.strong is False
    assert "helped" in result.message  # names the external blocker


def test_no_soft_lock_when_the_trap_exit_is_ungated() -> None:
    quest = _softlock_quest()
    for branch in quest.logic.branches:  # type: ignore[union-attr]
        if branch.id == "b_exit":
            branch.condition = ""
    result = check_soft_lock(quest)
    assert result.status is SpecStatus.PROVEN
    assert result.holds is True and result.strong is True


def test_soft_lock_on_unavailable_quest_is_vacuously_true() -> None:
    quest = _quest(
        "q_gated",
        ["s0"],
        QuestLogic(
            variables=[LogicVar(id="allowed", type=LogicVarType.BOOL, default=False)],
            precondition="allowed",
        ),
    )
    result = check_soft_lock(quest)
    assert result.holds is True and result.status is SpecStatus.PROVEN


# --------------------------------------------------------------------------------------------------
# AG p — state invariants (mutual exclusion, monotonic flags).
# --------------------------------------------------------------------------------------------------


def _mutex_quest(set_both: bool) -> Quest:
    effects = [Effect(var="a", op="set", value=True)]
    if set_both:
        effects.append(Effect(var="b", op="set", value=True))
    return _quest(
        "q_mutex",
        ["s0", "s1"],
        QuestLogic(
            variables=[
                LogicVar(id="a", type=LogicVarType.BOOL, default=False),
                LogicVar(id="b", type=LogicVarType.BOOL, default=False),
            ],
            stage_logic=[StageLogic(stage_id="s0", effects_on_complete=effects)],
            branches=[
                Branch(id="adv", from_stage="s0", to_stage="s1"),
                Branch(id="fin", from_stage="s1", outcome="done"),
            ],
        ),
    )


def test_ag_mutual_exclusion_violation_is_caught_with_witness() -> None:
    spec = TemporalSpec(id="mutex", pattern=PropertyPattern.AG, expression="not (a and b)")
    result = check_temporal_spec(_mutex_quest(set_both=True), spec)
    assert result.status is SpecStatus.VIOLATED
    assert result.holds is False
    assert result.counterexample_witness == ["adv"]


def test_ag_mutual_exclusion_holds_is_a_proof_when_exhaustive() -> None:
    spec = TemporalSpec(id="mutex", pattern=PropertyPattern.AG, expression="not (a and b)")
    result = check_temporal_spec(_mutex_quest(set_both=False), spec)
    assert result.status is SpecStatus.PROVEN
    assert result.holds is True and result.strong is True


def test_ag_violation_witness_replays_through_simulator() -> None:
    quest = _mutex_quest(set_both=True)
    spec = TemporalSpec(id="mutex", pattern=PropertyPattern.AG, expression="not (a and b)")
    result = check_temporal_spec(quest, spec)
    run = simulate_quest(quest, choices=result.counterexample_witness)
    assert run.status == "completed"
    assert result.counterexample_stage in run.path


# --------------------------------------------------------------------------------------------------
# EF p — reachability.
# --------------------------------------------------------------------------------------------------


def _reach_quest() -> Quest:
    return _quest(
        "q_ef",
        ["s0", "s1"],
        QuestLogic(
            variables=[LogicVar(id="got", type=LogicVarType.BOOL, default=False)],
            stage_logic=[
                StageLogic(
                    stage_id="s0", effects_on_complete=[Effect(var="got", op="set", value=True)]
                )
            ],
            branches=[
                Branch(id="adv", from_stage="s0", to_stage="s1"),
                Branch(id="fin", from_stage="s1", condition="got", outcome="done"),
            ],
        ),
    )


def test_ef_reachable_returns_a_positive_witness() -> None:
    spec = TemporalSpec(id="reach", pattern=PropertyPattern.EF, expression="got")
    result = check_temporal_spec(_reach_quest(), spec)
    assert result.holds is True and result.status is SpecStatus.PROVEN
    assert result.counterexample_witness == ["adv"]


def test_ef_unreachable_is_proven_false_when_exhaustive() -> None:
    # A predicate that no reachable state satisfies (a flag never set true).
    quest = _quest(
        "q_ef_never",
        ["s0"],
        QuestLogic(variables=[LogicVar(id="never", type=LogicVarType.BOOL, default=False)]),
    )
    spec = TemporalSpec(id="reach", pattern=PropertyPattern.EF, expression="never")
    result = check_temporal_spec(quest, spec)
    assert result.holds is False and result.status is SpecStatus.VIOLATED and result.strong is True


# --------------------------------------------------------------------------------------------------
# Int domain honesty — declared range converts a weak result into a strong proof.
# --------------------------------------------------------------------------------------------------


def _counter_loop_quest() -> Quest:
    """s0→s1, s1 increments ``n`` and may loop back to s0 (unbounded) or finish when n>=2."""
    return _quest(
        "q_counter",
        ["s0", "s1"],
        QuestLogic(
            variables=[LogicVar(id="n", type=LogicVarType.INT, default=0)],
            stage_logic=[
                StageLogic(stage_id="s1", effects_on_complete=[Effect(var="n", op="inc", value=1)])
            ],
            branches=[
                Branch(id="to1", from_stage="s0", to_stage="s1"),
                Branch(id="loop", from_stage="s1", to_stage="s0", condition="n < 100000"),
                Branch(id="win", from_stage="s1", condition="n >= 2", outcome="done"),
            ],
        ),
    )


def test_unbounded_int_truncates_to_a_weak_result() -> None:
    spec = TemporalSpec(id="floor", pattern=PropertyPattern.AG, expression="n >= 0")
    result = check_temporal_spec(_counter_loop_quest(), spec, max_states=500)
    assert result.truncated is True
    assert result.status is SpecStatus.NO_COUNTEREXAMPLE_IN_BOUND
    assert result.strong is False  # honest: cannot claim "proven"


def test_declared_int_range_yields_a_strong_proof() -> None:
    spec = TemporalSpec(
        id="floor", pattern=PropertyPattern.AG, expression="n >= 0", int_ranges={"n": (0, 3)}
    )
    result = check_temporal_spec(_counter_loop_quest(), spec, max_states=500)
    assert result.truncated is False
    assert result.status is SpecStatus.PROVEN and result.strong is True


# --------------------------------------------------------------------------------------------------
# Vacuity — an implication invariant whose trigger is never reachable is suspicious, not green.
# --------------------------------------------------------------------------------------------------


def test_vacuous_invariant_is_flagged_not_passed() -> None:
    quest = _quest(
        "q_vac",
        ["s0", "s1"],
        QuestLogic(
            variables=[
                LogicVar(id="trigger", type=LogicVarType.BOOL, default=False),
                LogicVar(id="conseq", type=LogicVarType.BOOL, default=False),
            ],
            branches=[
                Branch(id="adv", from_stage="s0", to_stage="s1"),
                Branch(id="fin", from_stage="s1", outcome="done"),
            ],
        ),
    )
    spec = TemporalSpec(
        id="impl", pattern=PropertyPattern.AG,
        expression="not trigger or conseq", antecedent="trigger",
    )
    result = check_temporal_spec(quest, spec)
    assert result.status is SpecStatus.VACUOUS
    assert result.holds is True and result.strong is False


# --------------------------------------------------------------------------------------------------
# Cone-of-influence slicing — hundreds of flags collapse to the handful that gate control flow.
# --------------------------------------------------------------------------------------------------


def _wide_flag_quest(n_notes: int) -> Quest:
    variables = [
        LogicVar(id=f"note_{i}", type=LogicVarType.BOOL, default=False) for i in range(n_notes)
    ]
    variables += [
        LogicVar(id="key", type=LogicVarType.BOOL, default=False),
        LogicVar(id="door", type=LogicVarType.BOOL, default=False),
    ]
    stage_logic = []
    for i in range(4):
        effects = [Effect(var=f"note_{i * 3 + k}", op="set", value=True) for k in range(3)]
        if i == 0:
            effects.append(Effect(var="key", op="set", value=True))
        if i == 1:
            effects.append(Effect(var="door", op="set", value=True))
        stage_logic.append(StageLogic(stage_id=f"s{i}", effects_on_complete=effects))
    branches = []
    for i in range(4):
        branches.append(Branch(id=f"adv{i}", from_stage=f"s{i}", to_stage=f"s{i + 1}"))
        branches.append(
            Branch(id=f"jump{i}", from_stage=f"s{i}", to_stage="s4", condition="key and door")
        )
    branches.append(Branch(id="fin", from_stage="s4", outcome="done"))
    logic = QuestLogic(variables=variables, stage_logic=stage_logic, branches=branches)
    return _quest("q_wide", [f"s{i}" for i in range(5)], logic)


def test_cone_of_influence_keeps_only_guard_flags() -> None:
    quest = _wide_flag_quest(n_notes=200)
    logic = quest.logic
    assert logic is not None
    keep = cone_of_influence(logic, set())  # soft-lock: property refs empty → only guard flags
    assert keep == {"key", "door"}  # the 200 write-only note flags are sliced away


def test_slicing_collapses_the_state_space_for_a_wide_flag_quest() -> None:
    result = check_soft_lock(_wide_flag_quest(n_notes=200))
    assert result.status is SpecStatus.PROVEN
    assert result.slice_total_vars == 202
    assert set(result.slice_kept_vars) == {"key", "door"}
    assert result.slice_ratio < 0.02  # < 2% of flags kept
    assert result.states_explored < 20  # vs a raw 2^202 state space


# --------------------------------------------------------------------------------------------------
# Anti-divergence — the CTL engine's reachable endings agree with the unmodified explorer.
# --------------------------------------------------------------------------------------------------


def test_soft_lock_agrees_with_explorer_over_the_complexity_corpus() -> None:
    """The complexity corpus is constructed soft-lock-free (every decision stage has an
    unconditional advance branch to a completing sink). This is the anti-divergence guard: the CTL
    soft-lock verdict must agree with ``explore_quest``'s "some outcome is reachable" over the SAME
    transition relation.
    """
    profiles = load_complexity_profiles()
    for tier in ("support", "chapter", "multiline"):
        bundle = generate_complexity_bundle(profiles[tier], 20260711)
        for quest in bundle.quests.values():
            report = explore_quest(quest)
            some_outcome_reachable = any(o.reachable for o in report.outcomes)
            result = check_soft_lock(quest)
            # A soft-lock-free, fully-explored quest is proven + holds, and (agreeing with the
            # explorer) at least one outcome is reachable.
            assert result.status is SpecStatus.PROVEN, (tier, quest.id, result.status)
            assert result.holds is True
            assert some_outcome_reachable is True


# --------------------------------------------------------------------------------------------------
# AG EF real run over the evaluation worlds — the HONEST efficacy report.
# --------------------------------------------------------------------------------------------------


def test_ag_ef_over_evaluation_worlds_reports_no_soft_lock_honestly() -> None:
    """Run the soft-lock check over BOTH shipped evaluation worlds and record the honest outcome.

    The acceptance world's quests carry no logic layer (nothing to check); the complexity corpus is
    *constructed* to be completable (an unconditional advance branch at every decision stage). So no
    soft-lock exists to find here — and the checker correctly finds none. This is the honest,
    non-inflated result: the checker's soft-lock DETECTION is validated by the positive-control
    quest (``test_soft_lock_detected_with_replayable_witness``) and the unit tests above; its
    efficacy on *real* authored content awaits a real game corpus.
    """
    found = 0
    checked = 0
    # 1) Acceptance world — quests have no logic; soft-lock is vacuously true / skipped.
    for quest in build_acceptance_world().quests.values():
        result = check_soft_lock(quest)
        checked += 1
        if result.status is SpecStatus.VIOLATED:
            found += 1
    # 2) Complexity corpus — has real logic, but is soft-lock-free by construction.
    profiles = load_complexity_profiles()
    for tier in ("support", "chapter", "multiline"):
        for quest in generate_complexity_bundle(profiles[tier], 20260711).quests.values():
            result = check_soft_lock(quest)
            checked += 1
            if result.status is SpecStatus.VIOLATED:
                found += 1
    assert checked > 100  # both worlds were actually exercised
    assert found == 0  # honest: no typical soft-lock in these constructed-completable worlds


# --------------------------------------------------------------------------------------------------
# Planner-declared spec parsing.
# --------------------------------------------------------------------------------------------------


def test_parse_specs_from_quest_metadata() -> None:
    quest = _mutex_quest(set_both=True)
    quest.metadata["temporal_specs"] = [
        {"pattern": "AG", "expression": "not (a and b)", "description": "互斥"}
    ]
    specs = parse_specs_from_quest(quest)
    assert len(specs) == 1
    assert specs[0].pattern is PropertyPattern.AG
    assert specs[0].id == "q_mutex_spec0"  # auto-id when the entry omits one


def test_parse_specs_skips_malformed_entries() -> None:
    quest = _mutex_quest(set_both=False)
    quest.metadata["temporal_specs"] = ["not a dict", {"pattern": "NONSENSE"}, 42]
    assert parse_specs_from_quest(quest) == []
