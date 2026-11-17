# WS-E · 有界全路径探索（叙事模型检查）：结局可达性 / 死分支 / 覆盖率。
"""Bounded explicit-state exploration of a quest's branch graph.

The search enumerates reachable ``(stage, variable-state)`` pairs with a breadth-first
frontier and a visited set. It reuses the simulator's state seeding and condition checks,
and applies stage effects followed by the selected branch's effects. The report records
reachable outcomes, branch witnesses, coverage and dead ends within the explored bounds.

The explorer can revisit a stage with different variable values; the single-path simulator
instead detects cycles by stage. A witness that visits every stage at most once can be
replayed with ``simulate_quest(quest, choices=witness)``. Witnesses that revisit a stage
may stop as ``cycle`` in that simulator, even when the state-space explorer reaches an end.

``max_states`` and ``max_depth`` bound the search, including unbounded counter domains.
When ``truncated`` is true, absent outcomes and dead-branch lists mean not reached within
the explored boundary. They do not establish unreachability or replace human playtesting.
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, Field

from ..content.models import Quest
from .semantics import WorldState
from .simulate import Value, _seed_state, _truthy

# Generous but finite defaults: real quest graphs (a forward stage-DAG whose only state change is a
# handful of on-complete effects) explore fully well under these; they still bound a pathological
# unbounded-counter loop so the search always terminates.
_DEFAULT_MAX_STATES = 100_000
_DEFAULT_MAX_DEPTH = 10_000

StateKey = tuple[tuple[str, Value], ...]


class OutcomeReach(BaseModel):
    """One distinct terminal result of a quest and whether the search reached it.

    ``kind`` is ``"terminal"`` (a ``Branch`` with an empty ``to_stage`` — its ``branch_id`` and
    ``outcome`` label) or ``"linear"`` (the quest completes by reaching a last stage with no
    outgoing branches; label ``"completed"``). ``witness`` is the branch-id sequence that reaches
    it. Stage-simple witnesses replay with ``simulate_quest(quest, choices=witness)``;
    witnesses that revisit a stage have the simulator cycle-detection limitation above.
    Empty when not reached within the boundary.
    """

    outcome: str
    kind: str
    branch_id: str = ""
    reachable: bool = False
    witness: list[str] = Field(default_factory=list)


class DeadEnd(BaseModel):
    """A path that terminates without completing the quest.

    ``reason``: ``"unavailable"`` (the quest-level precondition was false — no stage was entered),
    ``"blocked"`` (a stage precondition was false), ``"deadlock"`` (a decision stage with no
    satisfiable branch), ``"dangling"`` (a branch pointing at a non-existent stage), or
    ``"no_terminal"`` (the whole explored region reached no outcome at all, e.g. a pure loop).
    """

    stage_id: str
    reason: str
    detail: str = ""


class ExplorationReport(BaseModel):
    """The diagnostic result of a bounded exploration of one quest's branch graph.

    ``truncated`` is the load-bearing honesty flag: when True the boundary (``max_states`` /
    ``max_depth``) was hit and the ``unreachable_stages`` / ``dead_branches`` / unreachable outcomes
    are "not reached within the boundary", not proven unreachable.
    """

    quest_id: str
    states_explored: int
    truncated: bool
    frontier_remaining: int
    depth_truncated_paths: int
    max_states: int
    max_depth: int
    outcomes: list[OutcomeReach] = Field(default_factory=list)
    reached_stages: list[str] = Field(default_factory=list)
    unreachable_stages: list[str] = Field(default_factory=list)
    stage_coverage: float = 1.0
    covered_branches: list[str] = Field(default_factory=list)
    dead_branches: list[str] = Field(default_factory=list)
    unreached_branches: list[str] = Field(default_factory=list)
    branch_coverage: float = 1.0
    dead_ends: list[DeadEnd] = Field(default_factory=list)


def _state_key(state: WorldState) -> StateKey:
    """Hashable, order-stable snapshot of a var-state for the visited set (SPIN-style dedup)."""
    return tuple(sorted(state.as_mapping().items()))


def explore_quest(
    quest: Quest,
    *,
    max_states: int = _DEFAULT_MAX_STATES,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> ExplorationReport:
    """Bounded explicit-state exploration of ``quest``'s branch graph.

    BFS over ``(stage, frozen var-state)`` nodes from the seeded first stage, taking EVERY
    satisfiable branch at each decision stage (unlike the single-path simulator). Deterministic:
    same quest =>
    byte-identical report. Reuses the simulator's seeding/condition/effect semantics so every
    reachable outcome's ``witness`` replays through ``simulate_quest`` unchanged.
    """
    logic = quest.logic
    stages = [s.id for s in quest.stages]
    if logic is None or not stages:
        # Mirror simulate_quest's trivially-completed path (no logic or no stages).
        return ExplorationReport(
            quest_id=quest.id,
            states_explored=len(stages),
            truncated=False,
            frontier_remaining=0,
            depth_truncated_paths=0,
            max_states=max_states,
            max_depth=max_depth,
            outcomes=[OutcomeReach(outcome="completed", kind="linear", reachable=True)],
            reached_stages=list(stages),
        )

    stage_set = set(stages)
    stage_logic = {sl.stage_id: sl for sl in logic.stage_logic}
    branches_from: dict[str, list] = {}
    for branch in logic.branches:
        branches_from.setdefault(branch.from_stage, []).append(branch)
    branch_from_stage = {b.id: b.from_stage for b in logic.branches}
    all_branch_ids = [b.id for b in logic.branches]

    # Statically enumerate every distinct outcome, then let BFS mark reachability + a witness.
    outcome_specs: dict[tuple[str, str], OutcomeReach] = {}
    for branch in logic.branches:
        if not branch.to_stage:  # empty to_stage => terminal outcome (matches _next_stage)
            outcome_specs[("terminal", branch.id)] = OutcomeReach(
                outcome=branch.outcome, kind="terminal", branch_id=branch.id
            )
    linear_key = ("linear", "")
    if stages[-1] not in branches_from:  # last stage has no branches => a linear completion exists
        outcome_specs[linear_key] = OutcomeReach(outcome="completed", kind="linear")

    seed_state = _seed_state(logic, None)
    if not _truthy(seed_state, logic.precondition):
        # Quest-level gate — mirrors simulate_quest's `unavailable`: the quest cannot be entered, so
        # nothing is explored and no outcome is reachable. A single `unavailable` dead end (carrying
        # the gate expression) explains it; the outcome list stays as-enumerated, all unreachable.
        return ExplorationReport(
            quest_id=quest.id,
            states_explored=0,
            truncated=False,
            frontier_remaining=0,
            depth_truncated_paths=0,
            max_states=max_states,
            max_depth=max_depth,
            outcomes=sorted(outcome_specs.values(), key=lambda o: (o.kind, o.branch_id, o.outcome)),
            reached_stages=[],
            unreachable_stages=sorted(stages),
            stage_coverage=0.0,
            covered_branches=[],
            dead_branches=[],
            unreached_branches=sorted(all_branch_ids),
            branch_coverage=0.0 if all_branch_ids else 1.0,
            dead_ends=[
                DeadEnd(
                    stage_id="",
                    reason="unavailable",
                    detail=f"任务前置不满足：{logic.precondition}",
                )
            ],
        )
    seed_key = _state_key(seed_state)
    frontier: deque[tuple[str, StateKey, list[str], int]] = deque([(stages[0], seed_key, [], 0)])
    visited: set[tuple[str, StateKey]] = set()

    covered: set[str] = set()
    reached_stages: set[str] = set()
    dead_ends: list[DeadEnd] = []
    states_explored = 0
    depth_truncated_paths = 0
    truncated = False

    while frontier and states_explored < max_states:
        pos, pre_key, witness, depth = frontier.popleft()
        node = (pos, pre_key)
        if node in visited:
            continue
        visited.add(node)
        states_explored += 1
        reached_stages.add(pos)

        sl = stage_logic.get(pos)
        pre_state = WorldState(dict(pre_key))
        if sl and not _truthy(pre_state, sl.precondition):
            dead_ends.append(
                DeadEnd(stage_id=pos, reason="blocked", detail=f"前置条件不满足：{sl.precondition}")
            )
            continue

        post_state = WorldState(dict(pre_key))
        if sl:
            for effect in sl.effects_on_complete:
                post_state.apply(effect.var, effect.op, effect.value)
        post_key = _state_key(post_state)

        outgoing = branches_from.get(pos, [])
        if not outgoing:
            index = stages.index(pos)
            if index + 1 < len(stages):  # linear advance carries no branch id into the witness
                new_depth = depth + 1
                if new_depth > max_depth:
                    depth_truncated_paths += 1
                    truncated = True
                    continue
                frontier.append((stages[index + 1], post_key, witness, new_depth))
            else:  # last stage, no branches => linear completion
                spec = outcome_specs.get(linear_key)
                if spec is not None and not spec.reachable:
                    spec.reachable = True
                    spec.witness = list(witness)
            continue

        satisfiable = [b for b in outgoing if _truthy(post_state, b.condition)]
        if not satisfiable:
            dead_ends.append(DeadEnd(stage_id=pos, reason="deadlock", detail="没有可满足的分支"))
            continue
        for branch in satisfiable:
            covered.add(branch.id)
            if not branch.to_stage:  # terminal branch => record the outcome + its witness
                spec = outcome_specs.get(("terminal", branch.id))
                if spec is not None and not spec.reachable:
                    spec.reachable = True
                    spec.witness = [*witness, branch.id]
                continue
            if branch.to_stage not in stage_set:  # dangling ref (matches simulate's blocked branch)
                dead_ends.append(
                    DeadEnd(
                        stage_id=pos,
                        reason="dangling",
                        detail=f"分支指向不存在的阶段：{branch.to_stage}",
                    )
                )
                continue
            new_depth = depth + 1
            if new_depth > max_depth:
                depth_truncated_paths += 1
                truncated = True
                continue
            # Taking a branch applies ITS effects (mirrors simulate), so each satisfiable branch
            # expands into its own successor var-state.
            if branch.effects:
                branch_state = WorldState(dict(post_key))
                for effect in branch.effects:
                    branch_state.apply(effect.var, effect.op, effect.value)
                succ_key = _state_key(branch_state)
            else:
                succ_key = post_key
            frontier.append((branch.to_stage, succ_key, [*witness, branch.id], new_depth))

    frontier_remaining = sum(1 for (p, k, _, _) in frontier if (p, k) not in visited)
    if frontier_remaining > 0:
        truncated = True
    if not any(spec.reachable for spec in outcome_specs.values()):
        dead_ends.append(
            DeadEnd(stage_id="", reason="no_terminal", detail="在探索边界内未发现任何可达结局")
        )

    not_covered = [bid for bid in all_branch_ids if bid not in covered]
    return ExplorationReport(
        quest_id=quest.id,
        states_explored=states_explored,
        truncated=truncated,
        frontier_remaining=frontier_remaining,
        depth_truncated_paths=depth_truncated_paths,
        max_states=max_states,
        max_depth=max_depth,
        outcomes=sorted(outcome_specs.values(), key=lambda o: (o.kind, o.branch_id, o.outcome)),
        reached_stages=sorted(reached_stages),
        unreachable_stages=sorted(s for s in stages if s not in reached_stages),
        stage_coverage=len(reached_stages) / len(stages),
        covered_branches=sorted(covered),
        dead_branches=sorted(b for b in not_covered if branch_from_stage[b] in reached_stages),
        unreached_branches=sorted(
            b for b in not_covered if branch_from_stage[b] not in reached_stages
        ),
        branch_coverage=(len(covered) / len(all_branch_ids)) if all_branch_ids else 1.0,
        dead_ends=sorted(dead_ends, key=lambda d: (d.stage_id, d.reason, d.detail)),
    )
