# 对白树的确定性行走与有界探索：对白选择读写与任务逻辑同一套状态。
"""Deterministic walk + bounded exploration of a dialogue tree against a logic ``WorldState``.

This closes the "a dialogue choice changes the world" loop (the Detroit/BG3 mechanic): a
``DialogueChoice`` is gated by ``condition`` (same expression DSL as quest logic, already audited by
``DialogueChoiceConditionRule``) and now applies ``effects`` (the same frozen ``Effect`` type) when
taken. Both walk and exploration reuse the simulator's primitives (``_truthy`` / ``WorldState``)
so the transition semantics cannot diverge from quest simulation.

Composition contract (deliberate — the content model binds a tree to a QUEST via ``quest_id``, not
to a stage, so this module invents no placement semantics): walk the tree first, then feed the
resulting ``final_state`` into ``simulate_quest`` / ``explore_quest`` as ``initial_state``. The
end-to-end closure — a dialogue choice sets a flag, a later stage precondition reads it, the quest
path changes — is the composition of the two calls, and is exactly what the tests assert.

Choice addressing: a queued selector is ``"<node_id>:<choice_index>"`` (a ``DialogueChoice`` has no
id of its own); without a matching queued selector the walk takes the FIRST condition-satisfiable
choice, mirroring ``simulate_quest``'s branch selection discipline.
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, Field

from ..content.models import DialogueChoice, DialogueTree
from .expr import parse_expr, refs_in
from .semantics import WorldState
from .simulate import Value, _truthy

# Dialogue trees are small hand-authored graphs; these bounds only stop pathological input.
_MAX_WALK_STEPS = 200
_DEFAULT_MAX_ENDINGS = 1_000

StateKey = tuple[tuple[str, Value], ...]


class DialogueWalkStep(BaseModel):
    node_id: str
    chosen: str = ""  # "<node_id>:<index>" of the taken choice, or "" for a linear next_node hop
    effects: list[str] = Field(default_factory=list)


class DialogueWalk(BaseModel):
    """One deterministic traversal: status ``completed`` | ``dangling`` | ``deadlock`` | ``cycle``.

    ``deadlock`` = a node whose choices are all unsatisfiable and has no ``next_node`` fallback;
    ``dangling`` = a link points at a node that does not exist (the audit's broken-link case).
    ``final_state`` is the mutated variable state — feed it into ``simulate_quest`` as
    ``initial_state``.
    """

    tree_id: str
    status: str
    path: list[str] = Field(default_factory=list)
    steps: list[DialogueWalkStep] = Field(default_factory=list)
    final_state: dict[str, Value] = Field(default_factory=dict)
    message: str = ""


class DialogueEnding(BaseModel):
    """One distinct way the tree can end: the choice-selector path and the resulting state."""

    path: list[str] = Field(default_factory=list)  # taken choice selectors, in order
    final_state: dict[str, Value] = Field(default_factory=dict)


class DialogueExploration(BaseModel):
    """Bounded enumeration of every condition-satisfiable way through the tree."""

    tree_id: str
    endings: list[DialogueEnding] = Field(default_factory=list)
    covered_choices: list[str] = Field(default_factory=list)
    dead_choices: list[str] = Field(default_factory=list)  # never satisfiable in explored space
    truncated: bool = False


def _seed(tree: DialogueTree, initial_state: dict[str, Value] | None) -> WorldState:
    """Mirror the quest simulator's seeding: condition refs default to False, then override."""
    values: dict[str, Value] = {}
    for node in tree.nodes.values():
        for choice in node.choices:
            if not choice.condition.strip():
                continue
            try:
                for ref in refs_in(parse_expr(choice.condition)):
                    values.setdefault(ref, False)
            except ValueError:
                continue  # a broken condition is unsatisfiable; the audit already flags it
    if initial_state:
        values.update(initial_state)
    return WorldState(values)


def _apply_choice(state: WorldState, step: DialogueWalkStep, choice: DialogueChoice) -> None:
    for effect in choice.effects:
        state.apply(effect.var, effect.op, effect.value)
        step.effects.append(f"{effect.var} {effect.op} {effect.value}")


def walk_dialogue_tree(
    tree: DialogueTree,
    *,
    choices: list[str] | None = None,
    initial_state: dict[str, Value] | None = None,
    max_steps: int = _MAX_WALK_STEPS,
) -> DialogueWalk:
    """Walk ``tree`` from its root, taking queued selectors first, else the first satisfiable
    choice, applying the taken choice's effects — the dialogue counterpart of ``simulate_quest``."""
    queued = list(choices or [])
    state = _seed(tree, initial_state)
    walk = DialogueWalk(tree_id=tree.id, status="completed")
    visited: set[str] = set()
    pos: str | None = tree.root_node or None

    for _ in range(max_steps):
        if pos is None:
            break
        if pos not in tree.nodes:
            walk.status, walk.message = "dangling", f"链接指向不存在的节点「{pos}」"
            break
        if pos in visited:
            walk.status, walk.message = "cycle", f"在节点「{pos}」检测到循环"
            break
        visited.add(pos)
        node = tree.nodes[pos]
        step = DialogueWalkStep(node_id=pos)
        walk.path.append(pos)

        if node.choices:
            chosen_index: int | None = None
            for index, choice in enumerate(node.choices):
                selector = f"{pos}:{index}"
                if selector in queued and _truthy(state, choice.condition):
                    chosen_index = index
                    queued.remove(selector)
                    break
            if chosen_index is None:
                chosen_index = next(
                    (i for i, c in enumerate(node.choices) if _truthy(state, c.condition)), None
                )
            if chosen_index is None:
                if node.next_node is None:
                    walk.steps.append(step)
                    walk.status, walk.message = "deadlock", f"节点「{pos}」没有可满足的选项"
                    break
                pos = node.next_node  # linear fallback when every choice is gated off
            else:
                choice = node.choices[chosen_index]
                step.chosen = f"{pos}:{chosen_index}"
                _apply_choice(state, step, choice)
                pos = choice.next_node
        else:
            pos = node.next_node
        walk.steps.append(step)

    walk.final_state = state.as_mapping()
    return walk


def explore_dialogue_tree(
    tree: DialogueTree,
    *,
    initial_state: dict[str, Value] | None = None,
    max_endings: int = _DEFAULT_MAX_ENDINGS,
) -> DialogueExploration:
    """Enumerate every condition-satisfiable path (BFS, dedup on ``(node, var-state)``), the
    dialogue counterpart of ``explore_quest``: distinct endings with their final states, choice
    coverage, and choices never satisfiable within the explored space."""
    all_selectors = [
        f"{node.id}:{i}" for node in tree.nodes.values() for i in range(len(node.choices))
    ]
    seed = _seed(tree, initial_state)
    exploration = DialogueExploration(tree_id=tree.id)
    covered: set[str] = set()
    endings: list[DialogueEnding] = []

    root = tree.root_node or None
    frontier: deque[tuple[str | None, StateKey, list[str], frozenset[str]]] = deque(
        [(root, tuple(sorted(seed.as_mapping().items())), [], frozenset())]
    )
    visited: set[tuple[str | None, StateKey]] = set()

    while frontier:
        pos, key, path, seen_nodes = frontier.popleft()
        if (pos, key) in visited:
            continue
        visited.add((pos, key))
        if pos is None or pos not in tree.nodes or pos in seen_nodes:
            # completed / dangling / cycle all end the path; endings record only the state+path
            if len(endings) >= max_endings:
                exploration.truncated = True
                continue
            endings.append(DialogueEnding(path=path, final_state=dict(key)))
            continue
        node = tree.nodes[pos]
        seen = seen_nodes | {pos}
        state = WorldState(dict(key))
        satisfiable = [(i, c) for i, c in enumerate(node.choices) if _truthy(state, c.condition)]
        if not satisfiable:
            frontier.append((node.next_node, key, path, seen))
            continue
        for index, choice in satisfiable:
            selector = f"{pos}:{index}"
            covered.add(selector)
            branch_state = WorldState(dict(key))
            for effect in choice.effects:
                branch_state.apply(effect.var, effect.op, effect.value)
            frontier.append(
                (
                    choice.next_node,
                    tuple(sorted(branch_state.as_mapping().items())),
                    [*path, selector],
                    seen,
                )
            )

    exploration.endings = sorted(endings, key=lambda e: e.path)
    exploration.covered_choices = sorted(covered)
    exploration.dead_choices = sorted(s for s in all_selectors if s not in covered)
    return exploration
