"""WS-E · deterministic playtest: walk a quest's logic to see if it can actually be played.

Reuses the WS-A evaluator + WorldState. Given an optional sequence of branch choices, it walks from
the first stage — checking each precondition, applying on-complete effects, taking branches — and
reports the path, the final variable state, and the outcome: completed / deadlock (stuck, no branch
satisfiable) / blocked (a precondition was false) / cycle. No model calls; fully reproducible.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..content.models import Quest, QuestLogic
from .expr import parse_expr, refs_in
from .semantics import WorldState, evaluate

Value = bool | int | str


class SimStep(BaseModel):
    stage_id: str
    precondition_ok: bool
    effects: list[str] = Field(default_factory=list)
    branch_taken: str = ""  # branch id, or "" for a linear advance / terminal


class SimRun(BaseModel):
    status: str  # "completed" | "deadlock" | "blocked" | "cycle"
    path: list[str] = Field(default_factory=list)
    steps: list[SimStep] = Field(default_factory=list)
    final_state: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


def _seed_state(logic: QuestLogic, initial: dict[str, Value] | None) -> WorldState:
    values: dict[str, Value] = {var.id: var.default for var in logic.variables}
    # any quest-state ref (quest:X.done) referenced by an expression defaults to False unless given
    for source in _all_expressions(logic):
        try:
            for ref in refs_in(parse_expr(source)):
                values.setdefault(ref, False)
        except ValueError:
            continue
    if initial:
        values.update(initial)
    return WorldState(values)


def _all_expressions(logic: QuestLogic) -> list[str]:
    out = [logic.precondition, *(s.precondition for s in logic.stage_logic)]
    out += [b.condition for b in logic.branches]
    return [s for s in out if s.strip()]


def _truthy(state: WorldState, source: str) -> bool:
    if not source.strip():
        return True
    try:
        return bool(evaluate(parse_expr(source), state.as_mapping()))
    except ValueError:
        return False  # a broken expression is not satisfiable (audit already flags it)


def simulate_quest(
    quest: Quest,
    *,
    choices: list[str] | None = None,
    initial_state: dict[str, Value] | None = None,
    max_steps: int = 200,
) -> SimRun:
    logic = quest.logic
    stages = [s.id for s in quest.stages]
    stage_set = set(stages)
    if logic is None or not stages:
        return SimRun(status="completed", path=stages, message="无逻辑层，按线性默认可完成")

    state = _seed_state(logic, initial_state)
    stage_logic = {sl.stage_id: sl for sl in logic.stage_logic}
    branches_from: dict[str, list] = {}
    for branch in logic.branches:
        branches_from.setdefault(branch.from_stage, []).append(branch)

    queued_choices = list(choices or [])
    run = SimRun(status="completed", final_state={})
    visited: set[str] = set()
    pos = stages[0]

    for _ in range(max_steps):
        if pos in visited:
            run.status, run.message = "cycle", f"在阶段 '{pos}' 检测到循环"
            break
        visited.add(pos)
        sl = stage_logic.get(pos)
        precondition_ok = _truthy(state, sl.precondition) if sl else True
        step = SimStep(stage_id=pos, precondition_ok=precondition_ok)
        if not precondition_ok:
            run.steps.append(step)
            run.path.append(pos)
            run.status, run.message = "blocked", f"阶段 '{pos}' 的前置不满足"
            break
        if sl:
            for effect in sl.effects_on_complete:
                state.apply(effect.var, effect.op, effect.value)
                step.effects.append(f"{effect.var} {effect.op} {effect.value}")

        outgoing = branches_from.get(pos, [])
        nxt, branch_id, terminal = _next_stage(outgoing, stages, pos, state, queued_choices)
        step.branch_taken = branch_id
        run.steps.append(step)
        run.path.append(pos)
        if terminal:
            break
        if nxt is None:
            run.status, run.message = "deadlock", f"阶段 '{pos}' 没有可走的分支"
            break
        if nxt not in stage_set:  # a branch pointing at a stage that doesn't exist (dangling ref)
            run.status, run.message = "blocked", f"分支指向不存在的阶段「{nxt}」（悬空引用）"
            break
        pos = nxt

    run.final_state = state.as_mapping()
    return run


def _next_stage(
    outgoing: list,
    stages: list[str],
    pos: str,
    state: WorldState,
    queued_choices: list[str],
) -> tuple[str | None, str, bool]:
    """Return (next_stage_id, branch_id, is_terminal)."""
    if outgoing:
        # prefer an explicitly queued choice for this stage; else the first satisfiable branch
        chosen = None
        for branch in outgoing:
            if branch.id in queued_choices and _truthy(state, branch.condition):
                chosen = branch
                queued_choices.remove(branch.id)
                break
        if chosen is None:
            chosen = next((b for b in outgoing if _truthy(state, b.condition)), None)
        if chosen is None:
            return None, "", False  # deadlock
        if chosen.to_stage:
            return chosen.to_stage, chosen.id, False
        return None, chosen.id, True  # terminal outcome
    # linear: advance to the next stage, or complete if this was the last
    index = stages.index(pos)
    if index + 1 < len(stages):
        return stages[index + 1], "", False
    return None, "", True  # completed
