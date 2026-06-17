"""Deterministic, bundle-free checks over a quest's logic layer: undefined variables, type errors,
unreachable stages, and deadlocks (no path to completion). The audit-rule wrapper in
``audit/rules/logic_rules.py`` adds the bundle-aware dangling-reference check and maps these to
Issues; keeping the core here lets WS-E (playtest) reuse it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..content.models import Quest
from .expr import parse_expr, refs_in
from .semantics import type_errors


@dataclass
class LogicIssue:
    code: str
    message: str
    ref: str = ""


def _expressions(quest: Quest) -> list[tuple[str, str]]:
    """(label, source) for every non-empty expression in the quest's logic."""
    logic = quest.logic
    assert logic is not None
    out: list[tuple[str, str]] = []
    if logic.precondition.strip():
        out.append(("quest precondition", logic.precondition))
    for stage in logic.stage_logic:
        if stage.precondition.strip():
            out.append((f"stage {stage.stage_id} precondition", stage.precondition))
    for branch in logic.branches:
        if branch.condition.strip():
            out.append((f"branch {branch.id} condition", branch.condition))
    return out


def audit_quest_logic(quest: Quest) -> list[LogicIssue]:
    """All deterministic logic problems for one quest (empty when ``quest.logic`` is None)."""
    if quest.logic is None:
        return []
    issues: list[LogicIssue] = []
    issues.extend(_expression_issues(quest))
    issues.extend(_reachability_issues(quest))
    return issues


def _expression_issues(quest: Quest) -> list[LogicIssue]:
    logic = quest.logic
    assert logic is not None
    symbols = {var.id: var.type.value for var in logic.variables}
    issues: list[LogicIssue] = []
    for label, source in _expressions(quest):
        try:
            tree = parse_expr(source)
        except ValueError as exc:
            issues.append(LogicIssue("LOGIC_SYNTAX_ERROR", f"{label}: {exc}", source))
            continue
        # quest-state refs (quest:*.done) are auto-bool; only true unknowns are undefined.
        _ = refs_in(tree)
        for error in type_errors(tree, symbols):
            code = "LOGIC_UNDEFINED_VAR" if error.startswith("undefined") else "LOGIC_TYPE_MISMATCH"
            issues.append(LogicIssue(code, f"{label}: {error}", source))
    return issues


def _reachability_issues(quest: Quest) -> list[LogicIssue]:
    logic = quest.logic
    assert logic is not None
    stage_ids = [stage.id for stage in quest.stages]
    if not stage_ids:
        return []
    branch_from: dict[str, list] = defaultdict(list)
    for branch in logic.branches:
        branch_from[branch.from_stage].append(branch)

    edges: dict[str, list[str]] = defaultdict(list)
    terminals: set[str] = set()
    for index, sid in enumerate(stage_ids):
        outgoing = branch_from.get(sid)
        if outgoing:  # branches define this stage's flow, suppressing the linear fall-through
            for branch in outgoing:
                if branch.to_stage:
                    edges[sid].append(branch.to_stage)
                elif branch.outcome:
                    terminals.add(sid)
        elif index + 1 < len(stage_ids):
            edges[sid].append(stage_ids[index + 1])
        else:
            terminals.add(sid)  # last stage with no branch completes the quest

    reachable = _bfs(stage_ids[0], edges)
    issues: list[LogicIssue] = []
    for sid in stage_ids[1:]:
        if sid not in reachable:
            issues.append(
                LogicIssue("LOGIC_UNREACHABLE_STAGE", f"stage '{sid}' is never reachable", sid)
            )
    if not (reachable & terminals):
        issues.append(
            LogicIssue("LOGIC_DEADLOCK", "no path from the first stage reaches a completion", "")
        )
    return issues


def _bfs(entry: str, edges: dict[str, list[str]]) -> set[str]:
    seen = {entry}
    queue = [entry]
    while queue:
        node = queue.pop()
        for nxt in edges.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen
