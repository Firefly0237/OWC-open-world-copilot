# 结构复杂度合成基准：按叙事复杂度画像的参数带,确定性地生成结构复杂度语料 bundle。
"""Deterministic, profile-parameterized synthetic corpora for structural-complexity benchmarking.

This is N2b: the generator that turns the N2a narrative-complexity profiles
(``benchmarks/evidence/narrative-complexity/complexity-profiles.json``, three tiers
``support``/``chapter``/``multiline``) into
audit-clean, simulatable :class:`ContentBundle`s whose *structure* — quest count, branch fan-out,
convergence, logic-variable population, cross-quest ref density, dialogue depth/width — matches each
tier's evidence-derived parameter bands. It scales narrative STRUCTURE, the gap
``scripts/run_scale_benchmark.py`` (which scales the QUANTITY of unit vectors) leaves open.

Method — three canonical references, adopted not invented:

* **LDBC Social Network Benchmark — Datagen** (ldbcouncil.org, the reference model for a benchmark
  data generator). Two principles are load-bearing: (1) determinism is first-class — "Datagen is
  deterministic ... which guarantees all Test Sponsors face the same dataset, making comparisons
  fair and results reproducible"; our byte-level rule is *same seed + same tier => the identical
  corpus*, tested via :func:`~owcopilot.content.hash.content_hash`. (2) ONE generator parameterized
  by a small set of scale knobs; our three tiers ARE the scale factors — a single
  :func:`generate_complexity_bundle` reads ``profile`` and nothing branches on tier name.

* **daggen / the Suter-Hunold layer-by-layer random-DAG generator** (arXiv:1902.05808, repo
  frs69wq/daggen) — the canonical algorithm for a random DAG with a *controlled shape*, which is
  exactly a quest's branch graph. Edges only ever point forward (to a later stage), so the graph is
  acyclic by construction — matching both the simulator's cycle check and the auditor's reachability
  pass. The mapping: ``stages_per_quest`` = vertex count; ``branch_factor`` = a decision stage's
  out-degree; ``convergence_rate`` = the share of branches that merge into a later stage
  (``to_stage``) rather than end in a terminal outcome; a forward "jump" branch may skip stages.

* **Python stdlib ``random`` — Notes on reproducibility** (docs.python.org). It documents the exact
  guarantee the byte-level rule needs: ``random.Random(seed).random()`` "will continue to produce
  the same sequence when the compatible seeder is given the same seed", cross-version-stable, with
  an independent per-instance stream. We use ONE :class:`random.Random` per generate call and never
  touch global state. This is a *deliberate* divergence from ``run_scale_benchmark.py``'s
  ``numpy.random.default_rng`` (numpy explicitly gives no cross-version stream guarantee — fine for
  its approximate latency numbers, wrong for a byte-identical serialized corpus).

The generated bundle stores only ints / strings / bools (the ``*_rate`` bands only ever DRIVE
integer counts, never getting serialized), so the content hash is fully platform-stable.

Honest structural note (not a deviation — a graph-theoretic fact, the daggen "density" reality): a
quest with ``n`` reachable stages needs at least ``n - 1`` merge branches to connect them (a
spanning tree), so with a mean decision out-degree ``b`` the merge share is bounded below by
``~1/b``. The generator therefore draws ``branch_factor`` from
``[max(band_lo, ceil(1/conv_hi)), band_hi]`` and ``convergence_rate`` from
``[max(band_lo, ~1/b), band_hi]``: both achieved values still land inside their profile bands, just
drawn from the jointly-feasible sub-rectangle. ``logic_var_count`` is a *bundle total* (its
``multiline`` derivation reasons about whole-game state), distributed across quests; every other
parameter reads exactly as the N2a mapping table specifies.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from ..audit.context import AuditContext
from ..audit.default_rules import build_default_rule_registry
from ..audit.runner import AuditRunner
from ..content.hash import content_hash
from ..content.models import (
    Branch,
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
    Entity,
    EntityType,
    LogicVar,
    LogicVarType,
    Quest,
    QuestEventReference,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from ..logic.explore import OutcomeReach, explore_quest
from ..logic.simulate import simulate_quest

_PROFILES_PATH = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "evidence"
    / "narrative-complexity"
    / "complexity-profiles.json"
)

Band = tuple[float, float]


class ComplexityProfile(BaseModel):
    """One tier's structural parameter bands, loaded from ``complexity-profiles.json``.

    Every field is a ``[lo, hi]`` band; names mirror the N2a mapping table so the generator reads
    them with no shim. ``logic_var_ratio_{bool,int,enum}`` are the three type-share bands."""

    tier: str
    label: str
    quest_count: Band
    stages_per_quest: Band
    branch_factor: Band
    convergence_rate: Band
    logic_var_count: Band
    logic_var_ratio_bool: Band
    logic_var_ratio_int: Band
    logic_var_ratio_enum: Band
    cross_quest_ref_density: Band
    dialogue_tree_depth: Band
    dialogue_tree_width: Band
    conditional_choice_ratio: Band


def load_complexity_profiles(path: Path | None = None) -> dict[str, ComplexityProfile]:
    """Load the three tiers from the shipped N2a JSON (the single source of truth for the bands)."""
    data = json.loads((path or _PROFILES_PATH).read_text(encoding="utf-8"))
    profiles: dict[str, ComplexityProfile] = {}
    for tier, prof in data["profiles"].items():
        params = prof["params"]
        ratio = params["logic_var_ratio"]
        profiles[tier] = ComplexityProfile(
            tier=tier,
            label=prof["label"],
            quest_count=tuple(params["quest_count"]),
            stages_per_quest=tuple(params["stages_per_quest"]),
            branch_factor=tuple(params["branch_factor"]),
            convergence_rate=tuple(params["convergence_rate"]),
            logic_var_count=tuple(params["logic_var_count"]),
            logic_var_ratio_bool=tuple(ratio["bool"]),
            logic_var_ratio_int=tuple(ratio["int"]),
            logic_var_ratio_enum=tuple(ratio["enum"]),
            cross_quest_ref_density=tuple(params["cross_quest_ref_density"]),
            dialogue_tree_depth=tuple(params["dialogue_tree_depth"]),
            dialogue_tree_width=tuple(params["dialogue_tree_width"]),
            conditional_choice_ratio=tuple(params["conditional_choice_ratio"]),
        )
    return profiles


# --------------------------------------------------------------------------------------------------
# Deterministic generation.
# --------------------------------------------------------------------------------------------------


def _draw_int(rng: random.Random, band: Band) -> int:
    return rng.randint(int(band[0]), int(band[1]))


def _draw_float(rng: random.Random, band: Band) -> float:
    return rng.uniform(band[0], band[1])


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


class _Pool:
    """The shared entity pool a corpus references (givers, locations, factions, events, items).

    Sized deterministically from ``quest_count`` — no RNG — so entity identity never depends on the
    stream position, keeping the corpus byte-stable as generation logic around it evolves."""

    def __init__(self, quest_count: int) -> None:
        self.factions = [f"fac_{i:04d}" for i in range(max(2, min(6, quest_count // 4 + 2)))]
        self.npcs = [f"npc_{i:04d}" for i in range(max(6, quest_count + 4))]
        self.locations = [f"loc_{i:04d}" for i in range(max(4, quest_count + 2))]
        self.events = [f"evt_{i:04d}" for i in range(max(2, quest_count // 3 + 2))]
        self.items = [f"item_{i:04d}" for i in range(max(2, quest_count // 4 + 2))]

    def add_to(self, bundle: ContentBundle) -> None:
        for fid in self.factions:
            bundle.entities[fid] = Entity(id=fid, name=f"Faction {fid}", type=EntityType.FACTION)
        for nid in self.npcs:
            bundle.entities[nid] = Entity(id=nid, name=f"NPC {nid}", type=EntityType.NPC)
        for lid in self.locations:
            bundle.entities[lid] = Entity(id=lid, name=f"Location {lid}", type=EntityType.LOCATION)
        for eid in self.events:
            bundle.entities[eid] = Entity(id=eid, name=f"Event {eid}", type=EntityType.EVENT)
        for iid in self.items:
            bundle.entities[iid] = Entity(id=iid, name=f"Item {iid}", type=EntityType.ITEM)


def _draw_var_type_counts(
    rng: random.Random, total: int, profile: ComplexityProfile
) -> tuple[int, int, int]:
    """Split ``total`` logic variables into (bool, int, enum) counts, each type's SHARE inside its
    profile band and summing to ``total``.

    Drawn as nested intervals so the three shares are jointly feasible (sum to 1): ``r_int`` from
    its band, then ``r_enum`` from the sub-interval that forces ``r_bool = 1 - r_int - r_enum`` back
    into the bool band. Integer counts are then clamped to each band's ``[ceil(lo*T), floor(hi*T)]``
    and the bool remainder nudged into range — so every achieved share is provably within band."""
    ib = profile.logic_var_ratio_int
    eb = profile.logic_var_ratio_enum
    bb = profile.logic_var_ratio_bool
    r_int = _draw_float(rng, ib)
    e_lo = max(eb[0], 1.0 - bb[1] - r_int)
    e_hi = min(eb[1], 1.0 - bb[0] - r_int)
    r_enum = rng.uniform(e_lo, e_hi) if e_lo <= e_hi else _draw_float(rng, eb)

    n_int = _clamp(round(r_int * total), math.ceil(ib[0] * total), math.floor(ib[1] * total))
    n_enum = _clamp(round(r_enum * total), math.ceil(eb[0] * total), math.floor(eb[1] * total))
    n_bool = total - n_int - n_enum

    lo_b, hi_b = math.ceil(bb[0] * total), math.floor(bb[1] * total)
    while n_bool < lo_b and n_int > math.ceil(ib[0] * total):
        n_int -= 1
        n_bool += 1
    while n_bool < lo_b and n_enum > math.ceil(eb[0] * total):
        n_enum -= 1
        n_bool += 1
    while n_bool > hi_b and n_int < math.floor(ib[1] * total):
        n_int += 1
        n_bool -= 1
    while n_bool > hi_b and n_enum < math.floor(eb[1] * total):
        n_enum += 1
        n_bool -= 1
    return n_bool, n_int, n_enum


def _make_var(var_id: str, kind: LogicVarType) -> LogicVar:
    if kind is LogicVarType.INT:
        return LogicVar(id=var_id, name=var_id, type=kind, default=0)
    if kind is LogicVarType.ENUM:
        return LogicVar(
            id=var_id, name=var_id, type=kind, default="e0", enum_values=["e0", "e1", "e2"]
        )
    return LogicVar(id=var_id, name=var_id, type=kind, default=False)


def _condition_for(var: LogicVar) -> str:
    """A type-correct boolean gate reading ``var`` (so a marked branch/choice is genuinely
    conditional, and never trips a logic type-mismatch)."""
    if var.type is LogicVarType.INT:
        return f"{var.id} >= 1"
    if var.type is LogicVarType.ENUM:
        return f'{var.id} == "e1"'
    return var.id


@dataclass
class _Capable:
    """A branch or dialogue choice that MAY carry a gate condition, paired with a var to read.

    A plain dataclass (not a model) so it holds the live ``Branch``/``DialogueChoice`` object by
    reference — the second pass mutates ``.condition`` in place and it shows up in the bundle.
    (Advance branches stay unconditional so the simulator always has a satisfiable next step.)"""

    condition_var: LogicVar
    branch: Branch | None = None
    choice: DialogueChoice | None = None


def _build_quest_graph(
    quest_id: str,
    n_stages: int,
    branch_factor: int,
    convergence: float,
    variables: list[LogicVar],
    pool: _Pool,
    rng: random.Random,
) -> tuple[Quest, list[_Capable]]:
    """Build one quest as a forward (acyclic) stage DAG in the daggen style.

    Stages ``s_0..s_{n-1}``. Every non-sink stage ``s_j`` is a decision stage with out-degree
    ``branch_factor``: branch 0 is the always-true **advance** merge to ``s_{j+1}`` (guarantees full
    reachability + completion), and the remaining ``branch_factor - 1`` slots are filled with
    forward **jump merges** or **terminal outcomes** to hit ``convergence`` (= merge share). The
    sink stage ``s_{n-1}`` has no branches, so it completes the quest and is the reachable terminal.
    Returns the quest plus its condition-capable branches (for the bundle conditional pass)."""
    stages = [
        QuestStage(
            id=f"{quest_id}_s{j}",
            summary=f"Stage {j} of {quest_id}",
            location=pool.locations[j % len(pool.locations)],
            required_entities=[pool.npcs[j % len(pool.npcs)]],
        )
        for j in range(n_stages)
    ]
    stage_ids = [s.id for s in stages]

    total_branches = branch_factor * (n_stages - 1)
    merges_target = round(convergence * total_branches)
    # advances already contribute (n_stages - 1) merges; the rest come from extra slots.
    extra_merges = _clamp(merges_target - (n_stages - 1), 0, (branch_factor - 1) * (n_stages - 1))

    branches: list[Branch] = []
    capable: list[_Capable] = []
    remaining_merges = extra_merges
    terminal_seq = 0
    for j in range(n_stages - 1):
        # branch 0: the always-true advance merge (unconditional — the simulator's guaranteed step).
        branches.append(
            Branch(id=f"{quest_id}_b{j}_adv", from_stage=stage_ids[j], to_stage=stage_ids[j + 1])
        )
        for slot in range(1, branch_factor):
            var = variables[(j + slot) % len(variables)]
            if remaining_merges > 0:
                remaining_merges -= 1
                target = stage_ids[min(n_stages - 1, j + 2 + slot)]
                branch = Branch(
                    id=f"{quest_id}_b{j}_j{slot}", from_stage=stage_ids[j], to_stage=target
                )
            else:
                terminal_seq += 1
                branch = Branch(
                    id=f"{quest_id}_b{j}_t{slot}",
                    from_stage=stage_ids[j],
                    outcome=f"end_{terminal_seq}",
                )
            branches.append(branch)
            capable.append(_Capable(condition_var=var, branch=branch))

    # One on-complete effect on the first stage plus one reputation effect on the first terminal
    # branch: exercises the effect / `rep:<faction>` audit path without changing the sim outcome.
    first = variables[0]
    if first.type is LogicVarType.INT:
        stage_effect = Effect(var=first.id, op="inc", value=1)
    elif first.type is LogicVarType.ENUM:
        stage_effect = Effect(var=first.id, op="set", value="e1")
    else:
        stage_effect = Effect(var=first.id, op="set", value=True)
    stage_logic = [StageLogic(stage_id=stage_ids[0], effects_on_complete=[stage_effect])]
    for branch in branches:
        if branch.to_stage == "" and branch.from_stage == stage_ids[0]:
            branch.effects = [Effect(var=f"rep:{pool.factions[0]}", op="inc", value=1)]
            break

    logic = QuestLogic(variables=variables, stage_logic=stage_logic, branches=branches)
    quest = Quest(
        id=quest_id,
        title=f"Quest {quest_id}",
        giver_npc=pool.npcs[0],
        location=pool.locations[0],
        objective=f"Complete quest {quest_id}.",
        localization_keys=[f"quest.{quest_id}.objective"],
        stages=stages,
        logic=logic,
    )
    return quest, capable


def _build_dialogue_tree(
    quest_id: str,
    depth: int,
    width: int,
    participants: list[str],
    variables: list[LogicVar],
) -> tuple[DialogueTree, list[_Capable]]:
    """A dialogue tree whose longest root->leaf path has ``depth`` nodes and whose branching nodes
    each offer ``width`` choices.

    A ``depth``-node spine carries the deepest path; each spine node hangs ``width - 1`` leaf nodes,
    so width is honoured without exponential blow-up (nodes ~ depth * width, not width**depth).
    Every node is reachable from the root, so the tree audits clean. Returns the tree plus its
    condition-capable choices."""
    nodes: dict[str, DialogueNode] = {}
    capable: list[_Capable] = []
    leaf_seq = 0
    for j in range(depth):
        node_id = f"{quest_id}_n{j}"
        speaker = participants[j % len(participants)]
        if j == depth - 1:
            nodes[node_id] = DialogueNode(id=node_id, speaker_id=speaker, text=f"Node {j}")
            continue
        choices = [
            DialogueChoice(text=f"advance {j}", next_node=f"{quest_id}_n{j + 1}"),
        ]
        for slot in range(1, width):
            leaf_id = f"{quest_id}_leaf{leaf_seq}"
            leaf_seq += 1
            nodes[leaf_id] = DialogueNode(
                id=leaf_id, speaker_id=participants[leaf_seq % len(participants)], text="leaf"
            )
            choice = DialogueChoice(text=f"option {j}.{slot}", next_node=leaf_id)
            choices.append(choice)
            var = variables[(j + slot) % len(variables)]
            capable.append(_Capable(condition_var=var, choice=choice))
        nodes[node_id] = DialogueNode(
            id=node_id, speaker_id=speaker, text=f"Node {j}", choices=choices
        )

    tree = DialogueTree(
        id=f"dtree_{quest_id}",
        quest_id=quest_id,
        participants=participants,
        root_node=f"{quest_id}_n0",
        nodes=nodes,
    )
    return tree, capable


def generate_complexity_bundle(profile: ComplexityProfile, seed: int) -> ContentBundle:
    """Deterministically generate a structural-complexity corpus for ``profile`` under ``seed``.

    Same ``(profile, seed)`` => byte-identical bundle (one ``random.Random(seed)`` stream, fixed
    draw order). The result audits to zero open errors and each quest simulates to ``completed``."""
    rng = random.Random(seed)
    bundle = ContentBundle()

    quest_count = _draw_int(rng, profile.quest_count)
    cross_ref_density = _draw_float(rng, profile.cross_quest_ref_density)
    conditional_ratio = _draw_float(rng, profile.conditional_choice_ratio)

    pool = _Pool(quest_count)
    pool.add_to(bundle)

    logic_var_total = max(_draw_int(rng, profile.logic_var_count), 2 * quest_count)
    n_bool, n_int, n_enum = _draw_var_type_counts(rng, logic_var_total, profile)
    type_stream = (
        [LogicVarType.BOOL] * n_bool + [LogicVarType.INT] * n_int + [LogicVarType.ENUM] * n_enum
    )
    base = logic_var_total // quest_count
    remainder = logic_var_total % quest_count
    per_quest_var_counts = [base + (1 if i < remainder else 0) for i in range(quest_count)]

    capable_units: list[_Capable] = []
    quest_ids = [f"q{i:04d}" for i in range(quest_count)]
    var_cursor = 0
    for i, quest_id in enumerate(quest_ids):
        var_count = per_quest_var_counts[i]
        variables = [
            _make_var(f"{quest_id}_v{k}", type_stream[var_cursor + k]) for k in range(var_count)
        ]
        var_cursor += var_count

        n_stages = _draw_int(rng, profile.stages_per_quest)
        # Joint feasible draw (see module docstring): branch_factor high enough that its 1/b merge
        # floor fits under the convergence ceiling, then convergence at or above that floor.
        bf_lo = max(int(profile.branch_factor[0]), math.ceil(1.0 / profile.convergence_rate[1]))
        branch_factor = rng.randint(bf_lo, int(profile.branch_factor[1]))
        conv_floor = (n_stages - 1) / (branch_factor * (n_stages - 1)) if n_stages > 1 else 0.0
        conv_lo = max(profile.convergence_rate[0], conv_floor)
        conv_hi = profile.convergence_rate[1]
        convergence = rng.uniform(conv_lo, conv_hi) if conv_lo <= conv_hi else conv_hi

        quest, quest_capable = _build_quest_graph(
            quest_id, n_stages, branch_factor, convergence, variables, pool, rng
        )
        bundle.quests[quest_id] = quest
        capable_units.extend(quest_capable)

        depth = _draw_int(rng, profile.dialogue_tree_depth)
        width = _draw_int(rng, profile.dialogue_tree_width)
        participants = [pool.npcs[i % len(pool.npcs)], pool.npcs[(i + 1) % len(pool.npcs)]]
        tree, tree_capable = _build_dialogue_tree(quest_id, depth, width, participants, variables)
        bundle.dialogue_trees[tree.id] = tree
        capable_units.extend(tree_capable)

    _apply_cross_quest_refs(bundle, quest_ids, pool, cross_ref_density, profile)
    _apply_conditions(bundle, capable_units, conditional_ratio, profile)
    return bundle


def _apply_cross_quest_refs(
    bundle: ContentBundle,
    quest_ids: list[str],
    pool: _Pool,
    density: float,
    profile: ComplexityProfile,
) -> None:
    """Wire ``density * quest_count`` cross-quest references — backward prerequisites (acyclic),
    forward unlocks, then quest->event references — so the achieved density lands in band."""
    quest_count = len(quest_ids)
    lo_c = math.ceil(profile.cross_quest_ref_density[0] * quest_count)
    hi_c = math.floor(profile.cross_quest_ref_density[1] * quest_count)
    target = _clamp(round(density * quest_count), lo_c, hi_c)

    added = 0
    for i in range(1, quest_count):
        if added >= target:
            break
        bundle.quests[quest_ids[i]].prerequisites.append(quest_ids[i - 1])
        added += 1
    for i in range(quest_count - 1):
        if added >= target:
            break
        logic = bundle.quests[quest_ids[i]].logic
        assert logic is not None
        logic.unlocks.append(quest_ids[i + 1])
        added += 1
    event_seq = 0
    while added < target:
        qid = quest_ids[event_seq % quest_count]
        eid = pool.events[event_seq % len(pool.events)]
        ref_id = f"qer_{event_seq:05d}"
        bundle.quest_event_refs[ref_id] = QuestEventReference(id=ref_id, quest_id=qid, event_id=eid)
        added += 1
        event_seq += 1


def _apply_conditions(
    bundle: ContentBundle,
    capable_units: list[_Capable],
    ratio: float,
    profile: ComplexityProfile,
) -> None:
    """Mark a ``ratio`` share of ALL branches+choices as conditional by gating the first
    ``ratio * total_units`` condition-capable units (advance branches, the guaranteed sim steps,
    are excluded from ``capable_units`` and stay unconditional). Clamped to the band's integer
    ``[ceil(lo*T), floor(hi*T)]`` so the achieved ratio is provably within band."""
    total_branches = sum(len(q.logic.branches) for q in bundle.quests.values() if q.logic)
    total_choices = sum(
        len(n.choices) for t in bundle.dialogue_trees.values() for n in t.nodes.values()
    )
    total_units = total_branches + total_choices
    lo_c = math.ceil(profile.conditional_choice_ratio[0] * total_units)
    hi_c = math.floor(profile.conditional_choice_ratio[1] * total_units)
    target = _clamp(round(ratio * total_units), lo_c, hi_c)
    for unit in capable_units[:target]:
        condition = _condition_for(unit.condition_var)
        if unit.branch is not None:
            unit.branch.condition = condition
        elif unit.choice is not None:
            unit.choice.condition = condition


# --------------------------------------------------------------------------------------------------
# Measurement — the achieved structural profile of a generated bundle.
# --------------------------------------------------------------------------------------------------


class ComplexityMeasurement(BaseModel):
    """The achieved structural profile of a bundle — the same ten parameters the N2a profile bands,
    measured back off the produced content so the benchmark can show achieved-vs-band."""

    quest_count: int
    stages_per_quest: float
    branch_factor: float
    convergence_rate: float
    logic_var_count: int
    logic_var_ratio: dict[str, float]
    cross_quest_ref_density: float
    dialogue_tree_depth: float
    dialogue_tree_width: float
    conditional_choice_ratio: float
    total_stages: int
    total_branches: int
    total_dialogue_nodes: int


def _tree_depth(tree: DialogueTree) -> int:
    """Longest root->leaf path length, in nodes (root counts as 1). The tree is acyclic by
    construction, so a memoized DAG longest-path is exact."""
    memo: dict[str, int] = {}

    def depth(node_id: str) -> int:
        if node_id not in tree.nodes:
            return 0
        if node_id in memo:
            return memo[node_id]
        memo[node_id] = 1  # guard against an accidental cycle: treat as depth-1 while recursing
        node = tree.nodes[node_id]
        nexts = [node.next_node] if node.next_node else []
        nexts += [c.next_node for c in node.choices if c.next_node]
        best = 1 + max((depth(nx) for nx in nexts), default=0)
        memo[node_id] = best
        return best

    return depth(tree.root_node)


def measure_complexity(bundle: ContentBundle) -> ComplexityMeasurement:
    """Measure the achieved structural profile, using the exact mappings from the N2a table."""
    quests = list(bundle.quests.values())
    quest_count = len(quests)

    total_stages = sum(len(q.stages) for q in quests)
    stages_per_quest = total_stages / quest_count if quest_count else 0.0

    all_branches: list[Branch] = [b for q in quests if q.logic for b in q.logic.branches]
    out_degrees: list[int] = []
    for quest in quests:
        if not quest.logic:
            continue
        grouped: dict[str, int] = {}
        for branch in quest.logic.branches:
            grouped[branch.from_stage] = grouped.get(branch.from_stage, 0) + 1
        out_degrees.extend(grouped.values())
    branch_factor = sum(out_degrees) / len(out_degrees) if out_degrees else 0.0
    merges = sum(1 for b in all_branches if b.to_stage)
    convergence_rate = merges / len(all_branches) if all_branches else 0.0

    all_vars = [v for q in quests if q.logic for v in q.logic.variables]
    logic_var_count = len(all_vars)
    type_counts = {"bool": 0, "int": 0, "enum": 0}
    for var in all_vars:
        type_counts[var.type.value] += 1
    logic_var_ratio = {
        k: (v / logic_var_count if logic_var_count else 0.0) for k, v in type_counts.items()
    }

    cross_refs = (
        sum(len(q.prerequisites) for q in quests)
        + sum(len(q.logic.unlocks) for q in quests if q.logic)
        + len(bundle.quest_event_refs)
    )
    cross_quest_ref_density = cross_refs / quest_count if quest_count else 0.0

    trees = list(bundle.dialogue_trees.values())
    depths = [_tree_depth(t) for t in trees]
    dialogue_tree_depth = sum(depths) / len(depths) if depths else 0.0
    tree_widths: list[float] = []
    total_dialogue_nodes = 0
    for tree in trees:
        total_dialogue_nodes += len(tree.nodes)
        widths = [len(n.choices) for n in tree.nodes.values() if n.choices]
        if widths:
            tree_widths.append(sum(widths) / len(widths))
    dialogue_tree_width = sum(tree_widths) / len(tree_widths) if tree_widths else 0.0

    all_choices = [c for t in trees for n in t.nodes.values() for c in n.choices]
    conditional = sum(1 for b in all_branches if b.condition.strip())
    conditional += sum(1 for c in all_choices if c.condition.strip())
    total_units = len(all_branches) + len(all_choices)
    conditional_choice_ratio = conditional / total_units if total_units else 0.0

    return ComplexityMeasurement(
        quest_count=quest_count,
        stages_per_quest=stages_per_quest,
        branch_factor=branch_factor,
        convergence_rate=convergence_rate,
        logic_var_count=logic_var_count,
        logic_var_ratio=logic_var_ratio,
        cross_quest_ref_density=cross_quest_ref_density,
        dialogue_tree_depth=dialogue_tree_depth,
        dialogue_tree_width=dialogue_tree_width,
        conditional_choice_ratio=conditional_choice_ratio,
        total_stages=total_stages,
        total_branches=len(all_branches),
        total_dialogue_nodes=total_dialogue_nodes,
    )


# --------------------------------------------------------------------------------------------------
# Benchmark — run the three tiers through audit + simulate, the way the acceptance world does.
# --------------------------------------------------------------------------------------------------


class ComplexityBenchmarkRow(BaseModel):
    """One tier's benchmark result: what was produced, whether it audits clean and fully simulates,
    and whether each achieved parameter lands inside its profile band."""

    tier: str
    label: str
    seed: int
    content_hash: str
    measurement: ComplexityMeasurement
    open_errors: int
    sim_completed: int
    sim_total: int
    in_band: dict[str, bool]

    @property
    def audit_clean(self) -> bool:
        return self.open_errors == 0

    @property
    def sim_all_completed(self) -> bool:
        return self.sim_completed == self.sim_total

    @property
    def all_in_band(self) -> bool:
        return all(self.in_band.values())


def _in_band(value: float, band: Band) -> bool:
    return band[0] <= value <= band[1]


def evaluate_complexity_bands(
    measurement: ComplexityMeasurement, profile: ComplexityProfile
) -> dict[str, bool]:
    """Per-parameter check that the achieved value sits inside the profile band."""
    return {
        "quest_count": _in_band(measurement.quest_count, profile.quest_count),
        "stages_per_quest": _in_band(measurement.stages_per_quest, profile.stages_per_quest),
        "branch_factor": _in_band(measurement.branch_factor, profile.branch_factor),
        "convergence_rate": _in_band(measurement.convergence_rate, profile.convergence_rate),
        "logic_var_count": _in_band(measurement.logic_var_count, profile.logic_var_count),
        "logic_var_ratio_bool": _in_band(
            measurement.logic_var_ratio["bool"], profile.logic_var_ratio_bool
        ),
        "logic_var_ratio_int": _in_band(
            measurement.logic_var_ratio["int"], profile.logic_var_ratio_int
        ),
        "logic_var_ratio_enum": _in_band(
            measurement.logic_var_ratio["enum"], profile.logic_var_ratio_enum
        ),
        "cross_quest_ref_density": _in_band(
            measurement.cross_quest_ref_density, profile.cross_quest_ref_density
        ),
        "dialogue_tree_depth": _in_band(
            measurement.dialogue_tree_depth, profile.dialogue_tree_depth
        ),
        "dialogue_tree_width": _in_band(
            measurement.dialogue_tree_width, profile.dialogue_tree_width
        ),
        "conditional_choice_ratio": _in_band(
            measurement.conditional_choice_ratio, profile.conditional_choice_ratio
        ),
    }


def run_complexity_tier(profile: ComplexityProfile, seed: int) -> ComplexityBenchmarkRow:
    """Generate one tier's corpus and run it through the production audit + simulator paths."""
    bundle = generate_complexity_bundle(profile, seed)
    runner = AuditRunner(build_default_rule_registry())
    open_errors = len(runner.run(AuditContext.from_bundle(bundle)).open_errors)

    completed = sum(
        1 for quest in bundle.quests.values() if simulate_quest(quest).status == "completed"
    )
    measurement = measure_complexity(bundle)
    return ComplexityBenchmarkRow(
        tier=profile.tier,
        label=profile.label,
        seed=seed,
        content_hash=content_hash(bundle),
        measurement=measurement,
        open_errors=open_errors,
        sim_completed=completed,
        sim_total=len(bundle.quests),
        in_band=evaluate_complexity_bands(measurement, profile),
    )


def run_complexity_benchmark(
    seed: int = 20260711, tiers: tuple[str, ...] = ("support", "chapter", "multiline")
) -> list[ComplexityBenchmarkRow]:
    """The fixed benchmark: each tier's corpus, audited clean and fully simulated, achieved-vs-band.

    Deterministic and ``$0`` (stdlib ``random`` + the pure audit/simulate paths — no model, no
    network). The default ``seed`` reproduces the committed report."""
    profiles = load_complexity_profiles()
    return [run_complexity_tier(profiles[tier], seed) for tier in tiers if tier in profiles]


# --------------------------------------------------------------------------------------------------
# N3 — run the bounded full-path explorer (narrative model checking) over the same corpus.
# --------------------------------------------------------------------------------------------------


class ExplorationBenchmarkRow(BaseModel):
    """One tier's exploration result: aggregate DETERMINISTIC coverage/reachability counters plus
    the anti-divergence self-check that every reachable outcome's witness replays through the
    unmodified simulator. Every field here is a pure function of ``(profile, seed)`` — no wall-time
    is recorded, so the row is byte-stable across runs and platforms."""

    tier: str
    label: str
    seed: int
    quest_count: int
    total_states_explored: int
    total_outcomes: int
    reachable_outcomes: int
    unreachable_outcomes: int
    total_dead_branches: int
    total_unreached_branches: int
    truncated_quests: int
    # Exhaustive only over STAGE-SIMPLE (forward-DAG) corpora: the N2 corpus has 0 backward/self
    # edges across all 128 quests, so a green ``witnesses_replay`` is a property of THIS corpus, not
    # a general invariant — a stage-revisiting witness cycle-aborts in the simulator (see
    # ``_witness_replays``; that closure is N4).
    witnesses_replay: bool


def _witness_replays(quest: Quest, outcome: object) -> bool:
    """Replay one reachable outcome's witness through the UNMODIFIED simulator and confirm it lands:
    status ``completed`` and — for a terminal outcome — the same terminal branch id (or ``''`` for a
    linear completion). This is the divergence guard: it proves the explorer's transition relation
    is the simulator's, and that witnesses are genuinely replayable inputs.

    Bounded, not universal: replay only holds for STAGE-SIMPLE (forward-DAG) witnesses. A witness
    that revisits a stage with a changed var-state (a counter-gated finale) is correctly reported
    reachable by the explorer but cycle-aborts here in the simulator's stage-space visited set; the
    N2 corpus is forward-DAG (0 backward/self edges over its 128 quests), so this guard stays
    exhaustive over it — but a green result is a corpus property, not a general proof (N4)."""
    assert isinstance(outcome, OutcomeReach)
    run = simulate_quest(quest, choices=list(outcome.witness))
    if run.status != "completed":
        return False
    last_branch = run.steps[-1].branch_taken if run.steps else ""
    expected = outcome.branch_id if outcome.kind == "terminal" else ""
    return last_branch == expected


def run_exploration_tier(
    profile: ComplexityProfile,
    seed: int,
    *,
    max_states: int = 100_000,
    max_depth: int = 10_000,
) -> ExplorationBenchmarkRow:
    """Explore every quest in one tier's corpus and aggregate the deterministic exploration stats.

    Reuses the production :func:`~owcopilot.logic.explore.explore_quest` and, for the reachable
    outcomes, the production :func:`~owcopilot.logic.simulate.simulate_quest` to verify witness
    replay — no logic is re-implemented here."""
    bundle = generate_complexity_bundle(profile, seed)
    quests = list(bundle.quests.values())

    total_states = 0
    total_outcomes = 0
    reachable = 0
    unreachable = 0
    dead_branches = 0
    unreached_branches = 0
    truncated_quests = 0
    witnesses_replay = True
    for quest in quests:
        report = explore_quest(quest, max_states=max_states, max_depth=max_depth)
        total_states += report.states_explored
        total_outcomes += len(report.outcomes)
        dead_branches += len(report.dead_branches)
        unreached_branches += len(report.unreached_branches)
        if report.truncated:
            truncated_quests += 1
        for outcome in report.outcomes:
            if not outcome.reachable:
                unreachable += 1
                continue
            reachable += 1
            if not _witness_replays(quest, outcome):
                witnesses_replay = False

    return ExplorationBenchmarkRow(
        tier=profile.tier,
        label=profile.label,
        seed=seed,
        quest_count=len(quests),
        total_states_explored=total_states,
        total_outcomes=total_outcomes,
        reachable_outcomes=reachable,
        unreachable_outcomes=unreachable,
        total_dead_branches=dead_branches,
        total_unreached_branches=unreached_branches,
        truncated_quests=truncated_quests,
        witnesses_replay=witnesses_replay,
    )


def run_exploration_benchmark(
    seed: int = 20260711, tiers: tuple[str, ...] = ("support", "chapter", "multiline")
) -> list[ExplorationBenchmarkRow]:
    """The fixed exploration benchmark: each tier's corpus run through the bounded explorer, with
    the witness-replay divergence guard. Deterministic and ``$0`` (pure explore/simulate paths)."""
    profiles = load_complexity_profiles()
    return [run_exploration_tier(profiles[tier], seed) for tier in tiers if tier in profiles]
