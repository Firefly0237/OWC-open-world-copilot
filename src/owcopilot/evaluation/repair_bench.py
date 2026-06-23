"""Benchmark: the MCTS repair planner vs a greedy baseline, over the SAME move space.

Honest finding (run ``owcopilot eval-repair``): on the deterministic fixers, repairs are largely
independent, so greedy — take the move that resolves the most errors *now* — already reaches the
optimum. MCTS matches it at equal plan length, at $0, and provably never does worse. MCTS earns its
keep only when candidates *interact* (e.g. once model-proposed fixes compete): the controlled
``interacting-trap`` world shows greedy's myopic best-first choice dead-ending (it grabs the biggest
immediate win, which forecloses a later fix) while MCTS's lookahead resolves everything.

The takeaway is the judgement, not a manufactured win: greedy for the pure-deterministic path, MCTS
once the candidate set is richer. The deterministic audit makes either planner's reward free.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..audit.context import AuditContext
from ..audit.default_rules import build_default_rule_registry
from ..audit.runner import AuditRunner
from ..content.models import ContentBundle, Quest
from ..patches.fixers import deterministic_candidates
from ..patches.models import PatchCandidate, PatchOp, PatchOperation
from ..patches.search import (
    CandidateProvider,
    RepairMove,
    RepairPlan,
    applicable_repair_moves,
    plan_repairs,
)


def greedy_repair(
    bundle: ContentBundle,
    audit_runner: AuditRunner,
    *,
    candidate_provider: CandidateProvider = deterministic_candidates,
    max_steps: int = 16,
) -> RepairPlan:
    """Myopic baseline: repeatedly apply the single shadow-valid move that resolves the most errors
    right now (ties: fewest ops, then id), until no move makes progress. Same move space as the MCTS
    planner — so any difference is the search, not the candidates. MCTS must never do worse."""
    current = bundle
    initial = _open_errors(current, audit_runner)
    errors = initial
    moves: list[RepairMove] = []
    for _ in range(max_steps):
        progressing = [
            option
            for option in applicable_repair_moves(
                current, audit_runner, candidate_provider=candidate_provider
            )
            if option[2] < errors
        ]
        if not progressing:
            break
        move, current, errors = min(
            progressing, key=lambda option: (option[2], len(option[0].ops), option[0].patch_id)
        )
        moves.append(move)
        if errors == 0:
            break
    return RepairPlan(
        moves=moves,
        initial_open_errors=initial,
        final_open_errors=errors,
        resolved_errors=initial - errors,
        iterations=0,
    )


class RepairComparison(BaseModel):
    world: str
    initial_errors: int
    greedy_resolved: int
    greedy_plan_len: int
    mcts_resolved: int
    mcts_plan_len: int
    mcts_beats_greedy: bool
    mcts_at_least_greedy: bool  # the invariant: the search never regresses below the baseline


def compare_planners(
    world: str,
    bundle: ContentBundle,
    audit_runner: AuditRunner,
    *,
    candidate_provider: CandidateProvider = deterministic_candidates,
) -> RepairComparison:
    greedy = greedy_repair(bundle, audit_runner, candidate_provider=candidate_provider)
    mcts = plan_repairs(bundle, audit_runner, candidate_provider=candidate_provider)
    return RepairComparison(
        world=world,
        initial_errors=greedy.initial_open_errors,
        greedy_resolved=greedy.resolved_errors,
        greedy_plan_len=len(greedy.moves),
        mcts_resolved=mcts.resolved_errors,
        mcts_plan_len=len(mcts.moves),
        mcts_beats_greedy=mcts.resolved_errors > greedy.resolved_errors,
        mcts_at_least_greedy=mcts.resolved_errors >= greedy.resolved_errors,
    )


def run_repair_benchmark() -> list[RepairComparison]:
    """The fixed benchmark: two independent-fix worlds (real fixers, expected tie at the optimum)
    plus one controlled interacting world where MCTS's lookahead strictly beats greedy."""
    runner = AuditRunner(build_default_rule_registry())
    return [
        compare_planners("independent-3", _independent_world(3), runner),
        compare_planners("independent-5", _independent_world(5), runner),
        compare_planners(
            "interacting-trap",
            _trap_world(),
            runner,
            candidate_provider=_trap_candidate_provider,
        ),
    ]


# --------------------------------------------------------------------------- worlds
def _independent_world(quest_count: int) -> ContentBundle:
    """``quest_count`` quests, each with a dangling giver_npc and no localization key — two
    independent, deterministically-fixable errors apiece. Both planners should clear all of them."""
    quests = {
        f"q{i}": Quest(
            id=f"q{i}",
            title=f"Quest {i}",
            giver_npc="npc_ghost",  # references a non-existent entity -> UNKNOWN_ENTITY_REF
            objective="Do the thing.",  # present, so the only errors are the giver + localization
        )
        for i in range(1, quest_count + 1)
    }
    return ContentBundle(quests=quests)


def _trap_world() -> ContentBundle:
    """Three quests, each only error is a dangling giver_npc. With ``_trap_candidate_provider`` the
    fixes interact: resolving q1+q2 together (the biggest immediate win) forecloses fixing q3."""
    quests = {
        qid: Quest(
            id=qid,
            title=qid,
            giver_npc="npc_ghost",
            objective="Do the thing.",
            localization_keys=[f"quest.{qid}.objective"],
        )
        for qid in ("q1", "q2", "q3")
    }
    return ContentBundle(quests=quests)


def _trap_candidate_provider(issue, bundle: ContentBundle) -> list[PatchCandidate]:
    """A controlled interacting candidate set. q1 offers a 'broad' fix (resolve q1+q2 now) and a
    'narrow' one (q1 only); q3 is fixable ONLY while q2 is still unfixed. So greedy grabs the broad
    fix — the biggest immediate win — and strands q3, while MCTS looks ahead and takes the narrow
    path that lets all three be resolved."""
    if issue.rule_code != "UNKNOWN_ENTITY_REF":
        return []
    quest_id = issue.target_ref.partition(":")[2]

    def remove(target: str) -> PatchOperation:
        return PatchOperation(op=PatchOp.REMOVE, path=f"/quests/{target}/giver_npc")

    if quest_id == "q1":
        return [
            PatchCandidate(ops=[remove("q1"), remove("q2")], rationale="broad: resolve q1+q2 now"),
            PatchCandidate(ops=[remove("q1")], rationale="narrow: resolve q1 only"),
        ]
    if quest_id == "q2":
        return [PatchCandidate(ops=[remove("q2")], rationale="resolve q2")]
    if quest_id == "q3":
        q2 = bundle.quests.get("q2")
        q2_still_dangling = bool(q2 and str(q2.giver_npc or "").strip())
        if q2_still_dangling:
            return [PatchCandidate(ops=[remove("q3")], rationale="resolve q3 (needs q2 unfixed)")]
        return []
    return []


def _open_errors(bundle: ContentBundle, audit_runner: AuditRunner) -> int:
    return len(audit_runner.run(AuditContext.from_bundle(bundle)).open_errors)
