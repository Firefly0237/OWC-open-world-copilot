# WS-E · 切片式有界 CTL 时序检查：软锁(AG EF)/状态不变式(AG p)/可达(EF p)。
"""Sliced, bounded CTL model checking over a quest's flag state machine — the temporal-invariant
audit layer.

Where :mod:`owcopilot.logic.explore` reports *coverage/reachability* over the reachable state graph,
this module answers *temporal* questions the structural audit cannot see once flag semantics are in
play:

* ``AG EF <ending>`` — **soft-lock / no-dead-end**: from every reachable (stage, flag-state), some
  quest ending stays reachable. This is a branching (CTL) property with no LTL equivalent and is the
  flagship — it catches the semantic dead-end where a stage is structurally reachable but only via a
  flag combination from which no ending can ever be reached again.
* ``AG p`` — a **state invariant** that must hold in every reachable state (mutual exclusion
  ``AG not(a and b)``, a resource floor ``AG (gold >= 0)``, "a completed flag never regresses", …).
* ``EF p`` — **reachability**: some reachable state satisfies ``p`` (an ending is achievable, a flag
  combination is attainable).

Method — three deliberate reuses, invented nowhere:

* **Same transition relation as the simulator/explorer** (anti-divergence). The reachable state
  graph is built from the SAME primitives :mod:`.explore` uses — :func:`.simulate._seed_state`,
  :func:`.simulate._truthy`, and :meth:`.semantics.WorldState.apply` — so there is no second truth
  source. A cross-check test asserts this engine's reachable outcomes match ``explore_quest``.
* **Cone-of-influence slicing** (Clarke, state-explosion survey). A property over a handful of flags
  cannot depend on flags that no guard in this quest reads: those are write-only bookkeeping markers
  (read, if at all, by *other* quests). Projecting the state onto ``guard-vars ∪ property-vars``
  collapses ``2^(hundreds of flags)`` to ``2^(a handful)`` while preserving every reachable
  behaviour that the property or control flow can observe — a sound abstraction for these
  properties, not a heuristic.
* **CTL labeling with witness counterexamples** (Clarke-Emerson). ``EF``/soft-lock are decided by
  backward reachability over the built edge set; a violation is reported with the branch-id
  ``witness`` that reaches the offending state, replayable through ``simulate_quest`` for the
  forward-DAG shape of real quests (the same bounded guarantee :mod:`.explore` documents).

Honesty (load-bearing, mirrors :mod:`.explore`'s ``truncated`` contract):

* A ``violated`` verdict (a concrete reachable counterexample) is **sound even under truncation** —
  we found a real state. It is always reported when found.
* A ``holds`` verdict is a **proof only when the exploration was exhaustive** (``truncated=False``):
  then ``status="proven"`` / ``strong=True``. When the bound was hit, a clean run only means "no
  counterexample within the boundary" (``status="no_counterexample_in_bound"`` / ``strong=False``) —
  never "proven".
* Unbounded ``int`` (``inc``/``dec`` with no declared range) makes the raw state space infinite.
  Real quests are forward DAGs so their state space is finite anyway; a counter *loop* would
  truncate → weak result. Declaring an int range (``TemporalSpec.int_ranges``) turns that flag
  into a finite saturating domain, restoring an exhaustive run → a strong proof. This is the only
  lever that converts "no counterexample in bound" into "proven" for a looping counter.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, Field

from ..content.models import Quest, QuestLogic
from .expr import LogicSyntaxError, parse_expr, refs_in
from .semantics import LogicEvalError, Value, WorldState, evaluate
from .simulate import _seed_state, _truthy

# Same generous, finite bounds as :mod:`.explore` — a forward-DAG quest explores fully well under
# them; they still terminate a pathological unbounded-counter loop (reported honestly as truncated).
_DEFAULT_MAX_STATES = 100_000
_DEFAULT_MAX_DEPTH = 10_000

StateKey = tuple[tuple[str, Value], ...]
Node = tuple[str, StateKey]  # (stage_id, projected flag-state)


class PropertyPattern(str, Enum):
    """The CTL property patterns the A-tier engine decides.

    ``AG_EF_ENDING`` is the universal soft-lock property (no expression). ``AG``/``EF`` carry a
    boolean expression in the quest's expression language (see :mod:`.expr`).
    """

    AG = "AG"  # AG p — invariant: p holds in every reachable state
    EF = "EF"  # EF p — reachability: some reachable state satisfies p
    AG_EF_ENDING = "AG_EF_ENDING"  # AG EF ending — soft-lock freedom (no dead end)


class SpecStatus(str, Enum):
    """The honest verdict of one check (see module docstring)."""

    PROVEN = "proven"  # holds AND exhaustive → a proof
    NO_COUNTEREXAMPLE_IN_BOUND = "no_counterexample_in_bound"  # holds but truncated → weak
    VIOLATED = "violated"  # a concrete reachable counterexample was found (sound)
    VACUOUS = "vacuous"  # holds only because the antecedent is never reached (suspicious)
    WITHHELD = "withheld"  # a dead-end depends on an externally-set flag → cannot decide soundly
    UNSUPPORTED = "unsupported"  # a pattern/expression the A-tier engine does not decide
    ERROR = "error"  # the spec could not be parsed/evaluated against this quest


class TemporalSpec(BaseModel):
    """One temporal property to check against a quest.

    ``expression`` is a boolean-expression source (see :mod:`.expr`) for ``AG``/``EF``; it is empty
    for ``AG_EF_ENDING``. ``int_ranges`` optionally declares ``{flag: [lo, hi]}`` finite saturating
    domains for integer flags — the only way to earn a *strong* proof on a counter-looping quest.
    ``antecedent`` optionally names the trigger sub-expression of an implication invariant so the
    engine can run the vacuity check (an ``AG (not A or B)`` that holds only because ``A`` is never
    reachable is reported ``vacuous``, not green).
    """

    id: str = "spec"
    pattern: PropertyPattern
    expression: str = ""
    description: str = ""
    provenance: str = "planner"  # "builtin" (universal lint) | "planner" (declared/translated)
    int_ranges: dict[str, tuple[int, int]] = Field(default_factory=dict)
    antecedent: str = ""


class TemporalResult(BaseModel):
    """The verdict of checking one :class:`TemporalSpec` against one quest."""

    quest_id: str
    spec_id: str
    pattern: PropertyPattern
    holds: bool
    status: SpecStatus
    strong: bool = False  # True only when the verdict is a proof (exhaustive / declared-bounded)
    truncated: bool = False
    counterexample_witness: list[str] = Field(default_factory=list)
    counterexample_stage: str = ""
    counterexample_detail: str = ""
    slice_kept_vars: list[str] = Field(default_factory=list)
    slice_total_vars: int = 0
    states_explored: int = 0
    message: str = ""

    @property
    def slice_ratio(self) -> float:
        """Fraction of declared flags the slice kept (lower = more compression)."""
        total = self.slice_total_vars
        return (len(self.slice_kept_vars) / total) if total else 1.0


# --------------------------------------------------------------------------------------------------
# Cone-of-influence slicing.
# --------------------------------------------------------------------------------------------------


def _refs_of(source: str) -> set[str]:
    """Refs read by an expression source, or empty on empty/malformed source."""
    if not source.strip():
        return set()
    try:
        return refs_in(parse_expr(source))
    except LogicSyntaxError:
        return set()


def _guard_refs(logic: QuestLogic) -> set[str]:
    """Every flag read by a *guard* — the quest precondition, any stage precondition, any branch
    condition. These are exactly the flags that can steer control flow, so a sound slice must keep
    them all; a flag absent from this set is write-only within the quest and cannot affect any
    reachable behaviour the checker observes."""
    refs = _refs_of(logic.precondition)
    for stage in logic.stage_logic:
        refs |= _refs_of(stage.precondition)
    for branch in logic.branches:
        refs |= _refs_of(branch.condition)
    return refs


def cone_of_influence(logic: QuestLogic, property_refs: set[str]) -> set[str]:
    """The flags a property over ``property_refs`` can depend on: the property's own flags plus
    every guard flag. Sound for ``AG``/``EF``/soft-lock because a dropped flag neither steers flow
    nor appears in the property, so projecting it away merges only states that are indistinguishable
    to both. Effects have constant right-hand sides in this language, so no fixpoint is needed."""
    return set(property_refs) | _guard_refs(logic)


def _written_flags(logic: QuestLogic) -> set[str]:
    """Every flag the quest's own logic writes (stage on-complete effects + branch effects)."""
    written = {e.var for sl in logic.stage_logic for e in sl.effects_on_complete}
    written |= {e.var for b in logic.branches for e in b.effects}
    return written


def _external_input_flags(logic: QuestLogic) -> set[str]:
    """Flags a stage precondition or branch condition READS but no quest effect WRITES — i.e. state
    the quest does not control (a dialogue-set flag, a cross-quest ``quest:x.done``, an entry
    input). A dead-end blocked by such a flag is NOT a sound single-quest soft-lock: the owning
    dialogue / other quest may set it so the block clears. Read only from *internal* guards (stage
    preconditions + branch conditions); the quest-level precondition only gates entry."""
    read_in_guards: set[str] = set()
    for stage in logic.stage_logic:
        read_in_guards |= _refs_of(stage.precondition)
    for branch in logic.branches:
        read_in_guards |= _refs_of(branch.condition)
    return read_in_guards - _written_flags(logic)


# --------------------------------------------------------------------------------------------------
# Sliced reachable-state graph (same transition relation as explore/simulate).
# --------------------------------------------------------------------------------------------------


@dataclass
class _Graph:
    """The sliced reachable state graph plus the labels CTL checking needs."""

    initial: Node | None
    nodes: set[Node]
    adjacency: dict[Node, set[Node]]
    can_terminate: set[Node]  # nodes from which some ending fires directly (terminal / linear)
    node_state: dict[Node, dict[str, Value]]  # entry flag-state at each node
    witness: dict[Node, list[str]]  # a branch-id path reaching each node (BFS tree)
    terminal_states: list[tuple[dict[str, Value], list[str], str]]  # (post state, witness, stage)
    states_explored: int
    truncated: bool
    unavailable: bool  # quest-level precondition false → no reachable state
    range_violation: tuple[str, list[str], str] | None = None  # (detail, witness, stage)


def _project(state: WorldState, keep: set[str]) -> dict[str, Value]:
    return {k: v for k, v in state.as_mapping().items() if k in keep}


def _key(mapping: dict[str, Value]) -> StateKey:
    return tuple(sorted(mapping.items()))


def _apply_ranged(
    state: WorldState, var: str, op: str, value: Value, int_ranges: dict[str, tuple[int, int]]
) -> None:
    """Apply an effect, saturating into a declared int range when one is given. A declared range
    turns an ``inc``/``dec`` counter into a finite domain so enumeration terminates (a strong proof
    becomes possible); with no declared range this is exactly ``WorldState.apply``."""
    state.apply(var, op, value)
    bounds = int_ranges.get(var)
    if bounds is not None:
        current = state.values.get(var)
        if isinstance(current, int) and not isinstance(current, bool):
            lo, hi = bounds
            state.values[var] = max(lo, min(hi, current))


def _build_state_graph(
    quest: Quest,
    keep: set[str],
    *,
    int_ranges: dict[str, tuple[int, int]],
    extra_refs: set[str],
    max_states: int,
    max_depth: int,
) -> _Graph:
    """BFS over sliced ``(stage, projected flag-state)`` nodes, recording edges, per-node entry
    state, a witness path, which nodes can terminate directly, and the post-effect states at
    endings. Mirrors :func:`.explore.explore_quest`'s transition relation branch-by-branch."""
    logic = quest.logic
    stages = [s.id for s in quest.stages]
    assert logic is not None and stages

    stage_set = set(stages)
    stage_logic = {sl.stage_id: sl for sl in logic.stage_logic}
    branches_from: dict[str, list] = {}
    for branch in logic.branches:
        branches_from.setdefault(branch.from_stage, []).append(branch)

    seed = _seed_state(logic, None)
    for ref in extra_refs:  # property refs not read by any guard still need a seeded value
        seed.values.setdefault(ref, False)

    graph = _Graph(
        initial=None,
        nodes=set(),
        adjacency={},
        can_terminate=set(),
        node_state={},
        witness={},
        terminal_states=[],
        states_explored=0,
        truncated=False,
        unavailable=False,
    )

    if not _truthy(seed, logic.precondition):
        graph.unavailable = True
        return graph

    seed_proj = _project(seed, keep)
    initial: Node = (stages[0], _key(seed_proj))
    graph.initial = initial

    frontier: deque[tuple[Node, dict[str, Value], list[str], int]] = deque(
        [(initial, seed_proj, [], 0)]
    )
    visited: set[Node] = set()

    while frontier and graph.states_explored < max_states:
        node, pre_state_map, witness, depth = frontier.popleft()
        if node in visited:
            continue
        visited.add(node)
        graph.states_explored += 1
        graph.nodes.add(node)
        graph.adjacency.setdefault(node, set())
        graph.node_state[node] = dict(pre_state_map)
        graph.witness.setdefault(node, list(witness))

        pos = node[0]
        sl = stage_logic.get(pos)
        pre_state = WorldState(dict(pre_state_map))
        if sl and not _truthy(pre_state, sl.precondition):
            # A reachable stage whose own precondition is false: a dead state (no ending, no exit).
            continue

        post_state = WorldState(dict(pre_state_map))
        if sl:
            for effect in sl.effects_on_complete:
                if effect.var in keep:
                    _apply_ranged(post_state, effect.var, effect.op, effect.value, int_ranges)
        post_map = _project(post_state, keep)

        outgoing = branches_from.get(pos, [])
        if not outgoing:
            index = stages.index(pos)
            if index + 1 < len(stages):  # linear advance
                new_depth = depth + 1
                if new_depth > max_depth:
                    graph.truncated = True
                    continue
                succ: Node = (stages[index + 1], _key(post_map))
                graph.adjacency[node].add(succ)
                if succ not in visited:
                    frontier.append((succ, post_map, list(witness), new_depth))
            else:  # linear completion at the last stage
                graph.can_terminate.add(node)
                graph.terminal_states.append((dict(post_map), list(witness), pos))
            continue

        satisfiable = [b for b in outgoing if _truthy(post_state, b.condition)]
        if not satisfiable:
            continue  # deadlock state: no exit, no ending → a dead state (soft-lock candidate)
        for branch in satisfiable:
            if not branch.to_stage:  # terminal outcome
                graph.can_terminate.add(node)
                term_state = WorldState(dict(post_map))
                for effect in branch.effects:
                    if effect.var in keep:
                        _apply_ranged(term_state, effect.var, effect.op, effect.value, int_ranges)
                graph.terminal_states.append(
                    (_project(term_state, keep), [*witness, branch.id], pos)
                )
                continue
            if branch.to_stage not in stage_set:  # dangling ref → dead state
                continue
            new_depth = depth + 1
            if new_depth > max_depth:
                graph.truncated = True
                continue
            branch_state = WorldState(dict(post_map))
            for effect in branch.effects:
                if effect.var in keep:
                    _apply_ranged(branch_state, effect.var, effect.op, effect.value, int_ranges)
            succ_map = _project(branch_state, keep)
            succ = (branch.to_stage, _key(succ_map))
            graph.adjacency[node].add(succ)
            if succ not in visited:
                frontier.append((succ, succ_map, [*witness, branch.id], new_depth))

    if any((n not in visited) for n in _frontier_nodes(frontier)):
        graph.truncated = True
    return graph


def _frontier_nodes(frontier: deque[tuple[Node, dict[str, Value], list[str], int]]) -> list[Node]:
    return [item[0] for item in frontier]


# --------------------------------------------------------------------------------------------------
# CTL checks.
# --------------------------------------------------------------------------------------------------


def _reaches_ending(graph: _Graph) -> set[Node]:
    """Backward reachability: nodes from which some ending is reachable (``EF ending``). Seeded
    with the directly-terminating nodes, then propagated along reversed edges to a fixpoint."""
    reverse: dict[Node, set[Node]] = {n: set() for n in graph.nodes}
    for src, dsts in graph.adjacency.items():
        for dst in dsts:
            reverse.setdefault(dst, set()).add(src)
    reaching = set(graph.can_terminate)
    frontier = deque(graph.can_terminate)
    while frontier:
        node = frontier.popleft()
        for pred in reverse.get(node, ()):
            if pred not in reaching:
                reaching.add(pred)
                frontier.append(pred)
    return reaching


def _eval_pred(source: str, mapping: dict[str, Value]) -> bool | None:
    """Evaluate a boolean property at a state. ``None`` signals the expression could not be
    evaluated (unknown ref / type error) — a spec error, distinct from a ``False`` verdict."""
    try:
        result = evaluate(parse_expr(source), mapping)
    except (LogicSyntaxError, LogicEvalError):
        return None
    return bool(result)


def _empty_slice_info(logic: QuestLogic, keep: set[str]) -> tuple[list[str], int]:
    declared = {v.id for v in logic.variables}
    kept_declared = sorted(keep & declared)
    return kept_declared, len(declared)


def _trap_block_refs(quest: Quest, node: Node, entry_state: dict[str, Value]) -> set[str]:
    """The flags read by the guard(s) that immediately trap ``node`` (a dead node with no
    successor): a false stage precondition, or — for a decision stage with no satisfiable branch —
    the branch conditions that could, if satisfied, provide an exit. Used to decide whether the
    dead-end is sealed by quest-controlled flags (a sound soft-lock) or depends on an input flag."""
    logic = quest.logic
    assert logic is not None
    pos = node[0]
    stage_logic = {sl.stage_id: sl for sl in logic.stage_logic}
    sl = stage_logic.get(pos)
    state = WorldState(dict(entry_state))
    if sl and not _truthy(state, sl.precondition):
        return _refs_of(sl.precondition)
    outgoing = [b for b in logic.branches if b.from_stage == pos]
    return {ref for b in outgoing for ref in _refs_of(b.condition)}


def _classify_dead_region(
    quest: Quest, graph: _Graph, dead: list[Node], base: TemporalResult
) -> TemporalResult:
    """Decide, over an exhaustively-explored quest with a non-empty dead set, whether it is a sound
    soft-lock or must be withheld. A dead-end is a SOUND soft-lock only when the flags that seal it
    are all written by the quest itself; if any trap depends on an external input flag (a
    dialogue-set or cross-quest flag the quest never writes), the owning layer may clear the block,
    so a single-quest verdict is withheld rather than reported as a false positive."""
    logic = quest.logic
    assert logic is not None
    inputs = _external_input_flags(logic)
    # Trap sources = dead nodes with no successor (blocked precondition / deadlock / dangling only);
    # every other dead node inherits its deadness from one of these.
    trap_sources = [n for n in dead if not graph.adjacency.get(n)]

    def _key(n: Node) -> tuple[int, list[str], Node]:
        wit = graph.witness.get(n, [])
        return (len(wit), wit, n)

    sealed = [
        n for n in trap_sources
        if not (_trap_block_refs(quest, n, graph.node_state[n]) & inputs)
    ]
    if sealed:  # a genuine dead-end sealed by quest-controlled flags → a sound soft-lock
        worst = min(sealed, key=_key)
        base.holds = False
        base.status = SpecStatus.VIOLATED
        base.strong = True
        base.counterexample_witness = graph.witness.get(worst, [])
        base.counterexample_stage = worst[0]
        base.counterexample_detail = (
            f"在阶段 '{worst[0]}' 存在一个可达状态，从该状态再也走不到任何结局（软锁 / 死局）。"
        )
        base.message = "检测到软锁：存在无法通关的可达状态。"
        return base

    # Every dead-end depends on a flag this quest never writes (dialogue-set / cross-quest / input),
    # or the dead region has no identifiable trap source — cannot decide soundly, so withhold.
    base.status = SpecStatus.WITHHELD
    base.strong = False
    base.holds = True
    blockers = sorted(
        {
            ref
            for n in trap_sources
            for ref in (_trap_block_refs(quest, n, graph.node_state[n]) & inputs)
        }
    )
    detail = ("、".join(blockers)) if blockers else "无法定位死结点"
    base.message = (
        f"存在疑似死结点，但其阻断依赖任务自身从不写入的外部 flag（{detail}）—— "
        "可能由对白 / 其他任务置真；单任务软锁判定不可靠，故不下结论（诚实降级）。"
    )
    return base


def check_soft_lock(
    quest: Quest,
    *,
    max_states: int = _DEFAULT_MAX_STATES,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> TemporalResult:
    """``AG EF ending`` — decide whether every reachable state can still reach some quest ending.

    A violation (a reachable soft-lock state) is reported with the branch-id witness reaching it; it
    is sound only when the exploration was exhaustive (a truncated run cannot prove a state has NO
    onward ending, so a soft-lock verdict under truncation is withheld and reported as weak).
    """
    spec = TemporalSpec(
        id="soft_lock", pattern=PropertyPattern.AG_EF_ENDING, provenance="builtin",
        description="任何可达状态都仍能走到某个结局（无软锁 / 死局）。",
    )
    logic = quest.logic
    if logic is None or not quest.stages:
        return TemporalResult(
            quest_id=quest.id, spec_id=spec.id, pattern=spec.pattern, holds=True,
            status=SpecStatus.PROVEN, strong=True,
            message="任务无逻辑层或无阶段，按线性可完成，不存在软锁。",
        )
    keep = cone_of_influence(logic, set())
    kept, total = _empty_slice_info(logic, keep)
    graph = _build_state_graph(
        quest, keep, int_ranges={}, extra_refs=set(), max_states=max_states, max_depth=max_depth
    )
    base = TemporalResult(
        quest_id=quest.id, spec_id=spec.id, pattern=spec.pattern, holds=True,
        status=SpecStatus.PROVEN, truncated=graph.truncated,
        slice_kept_vars=kept, slice_total_vars=total, states_explored=graph.states_explored,
    )
    if graph.unavailable or graph.initial is None:
        base.message = "任务前置不满足，无可达状态，软锁性质空成立。"
        base.strong = True
        return base

    reaching = _reaches_ending(graph)
    dead = [n for n in graph.nodes if n not in reaching]
    if dead:
        if graph.truncated:
            # Truncated: cannot prove a suspected dead node has NO ending just past the boundary.
            base.status = SpecStatus.NO_COUNTEREXAMPLE_IN_BOUND
            base.strong = False
            base.message = "探索在边界内截断，疑似软锁无法证明为真正死局；结论降级为边界内未证。"
            return base
        return _classify_dead_region(quest, graph, dead, base)

    if graph.truncated:
        base.status = SpecStatus.NO_COUNTEREXAMPLE_IN_BOUND
        base.strong = False
        base.message = "探索在边界内截断：边界内未发现软锁，但未能穷尽证明。"
    else:
        base.strong = True
        base.message = "已证：所有可达状态都仍能走到结局，无软锁。"
    return base


def check_temporal_spec(
    quest: Quest,
    spec: TemporalSpec,
    *,
    max_states: int = _DEFAULT_MAX_STATES,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> TemporalResult:
    """Decide ``spec`` against ``quest``. ``AG_EF_ENDING`` routes to :func:`check_soft_lock`;
    ``AG``/``EF`` evaluate the sliced reachable state graph and return an honest verdict."""
    if spec.pattern is PropertyPattern.AG_EF_ENDING:
        result = check_soft_lock(quest, max_states=max_states, max_depth=max_depth)
        return result.model_copy(update={"spec_id": spec.id})

    logic = quest.logic
    if logic is None or not quest.stages:
        return TemporalResult(
            quest_id=quest.id, spec_id=spec.id, pattern=spec.pattern, holds=True,
            status=SpecStatus.PROVEN, strong=True,
            message="任务无逻辑层或无阶段：无状态可违反该性质。",
        )

    prop_refs = _refs_of(spec.expression)
    probe_fails = _eval_pred(spec.expression, _probe_state(logic, prop_refs)) is None
    if not spec.expression.strip() or probe_fails:
        return TemporalResult(
            quest_id=quest.id, spec_id=spec.id, pattern=spec.pattern, holds=False,
            status=SpecStatus.ERROR,
            message=f"规约表达式无法在该任务上解析/求值：{spec.expression!r}",
        )

    keep = cone_of_influence(logic, prop_refs)
    kept, total = _empty_slice_info(logic, keep)
    graph = _build_state_graph(
        quest, keep, int_ranges=spec.int_ranges, extra_refs=prop_refs,
        max_states=max_states, max_depth=max_depth,
    )
    base = TemporalResult(
        quest_id=quest.id, spec_id=spec.id, pattern=spec.pattern, holds=True,
        status=SpecStatus.PROVEN, truncated=graph.truncated,
        slice_kept_vars=kept, slice_total_vars=total, states_explored=graph.states_explored,
    )
    if graph.unavailable or graph.initial is None:
        base.strong = True
        base.message = "任务前置不满足，无可达状态；性质空成立。"
        return base

    all_states = [(n, graph.node_state[n], graph.witness.get(n, [])) for n in graph.nodes]
    all_states += [((stg, ()), st, wit) for (st, wit, stg) in graph.terminal_states]
    # Deterministic order (graph.nodes is a set): shortest witness first, then witness, then node —
    # so the FIRST violating/satisfying state encountered is the canonical, reproducible one.
    all_states.sort(key=lambda item: (len(item[2]), item[2], item[0]))

    if spec.pattern is PropertyPattern.AG:
        return _finish_ag(base, spec, all_states, graph)
    return _finish_ef(base, spec, all_states, graph)


def _probe_state(logic: QuestLogic, prop_refs: set[str]) -> dict[str, Value]:
    seed = _seed_state(logic, None)
    for ref in prop_refs:
        seed.values.setdefault(ref, False)
    return seed.as_mapping()


def _finish_ag(
    base: TemporalResult,
    spec: TemporalSpec,
    all_states: list[tuple[Node, dict[str, Value], list[str]]],
    graph: _Graph,
) -> TemporalResult:
    """``AG p`` — p must hold in every reachable (and terminal) state; a failing state is a sound
    counterexample. A clean run is a proof only when exhaustive."""
    # all_states is pre-sorted shortest-witness-first, so the first failing state is canonical.
    violating: tuple[Node, list[str]] | None = None
    for node, state, witness in all_states:
        if _eval_pred(spec.expression, state) is False:
            violating = (node, witness)
            break
    if violating is not None:
        node, witness = violating
        base.holds = False
        base.status = SpecStatus.VIOLATED
        base.strong = True  # a concrete reachable counterexample is sound even under truncation
        base.counterexample_witness = witness
        base.counterexample_stage = node[0]
        base.counterexample_detail = (
            f"在阶段 '{node[0]}' 的一个可达状态下，不变式 {spec.expression!r} 不成立。"
        )
        base.message = "不变式被违反：存在使其为假的可达状态。"
        return base

    # holds: check for vacuity (an implication invariant whose trigger is never reachable) before
    # declaring a clean pass, so a mis-grounded spec reads as suspicious rather than green.
    if spec.antecedent.strip():
        trigger_reachable = any(
            _eval_pred(spec.antecedent, state) is True for _n, state, _w in all_states
        )
        if not trigger_reachable and not graph.truncated:
            base.status = SpecStatus.VACUOUS
            base.strong = False
            base.message = (
                f"不变式在所有可达状态成立，但其触发前件 {spec.antecedent!r} 从未被触达 —— "
                "可能是符号接地错误导致的『空泛满足』，请人工复核。"
            )
            return base

    if graph.truncated:
        base.status = SpecStatus.NO_COUNTEREXAMPLE_IN_BOUND
        base.strong = False
        base.message = "探索在边界内截断：边界内未发现反例，但未能穷尽证明。"
    else:
        base.strong = True
        base.message = "已证：不变式在所有可达状态成立。"
    return base


def _finish_ef(
    base: TemporalResult,
    spec: TemporalSpec,
    all_states: list[tuple[Node, dict[str, Value], list[str]]],
    graph: _Graph,
) -> TemporalResult:
    """``EF p`` — some reachable state satisfies p. A positive witness proves reachability (sound
    even under truncation); absence is proof of unreachability only when exhaustive."""
    for node, state, witness in all_states:
        if _eval_pred(spec.expression, state) is True:
            base.holds = True
            base.strong = True
            base.status = SpecStatus.PROVEN
            base.counterexample_witness = witness  # here: the positive witness that reaches p
            base.counterexample_stage = node[0]
            base.counterexample_detail = (
                f"在阶段 '{node[0]}' 可达一个满足 {spec.expression!r} 的状态。"
            )
            base.message = "可达性成立：存在满足该性质的可达状态（附见证路径）。"
            return base
    # p never seen.
    if graph.truncated:
        base.holds = False
        base.status = SpecStatus.NO_COUNTEREXAMPLE_IN_BOUND
        base.strong = False
        base.message = "探索在边界内截断：边界内未发现满足该性质的状态，但不能断言不可达。"
    else:
        base.holds = False
        base.status = SpecStatus.VIOLATED
        base.strong = True
        base.counterexample_detail = f"穷尽可达状态后，{spec.expression!r} 在任何状态都不成立。"
        base.message = "可达性不成立：已证该性质在所有可达状态均为假（不可达）。"
    return base


# --------------------------------------------------------------------------------------------------
# Planner-declared specs live in quest metadata (same home other rules read, e.g. lore_rules).
# --------------------------------------------------------------------------------------------------

_SPEC_METADATA_KEY = "temporal_specs"


def parse_specs_from_quest(quest: Quest) -> list[TemporalSpec]:
    """Read the planner-declared / confirmed temporal specs stored on ``quest.metadata`` under
    ``temporal_specs`` (a list of dicts). Malformed entries are skipped, never raised — a bad spec
    must not break the whole audit run."""
    raw = quest.metadata.get(_SPEC_METADATA_KEY)
    if not isinstance(raw, list):
        return []
    specs: list[TemporalSpec] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        try:
            spec = TemporalSpec.model_validate(entry)
        except Exception:
            continue
        if not spec.id or spec.id == "spec":
            spec = spec.model_copy(update={"id": f"{quest.id}_spec{index}"})
        specs.append(spec)
    return specs
