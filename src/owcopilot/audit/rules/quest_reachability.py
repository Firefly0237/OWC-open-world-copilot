"""Global cross-quest state-propagation reachability audit rule (IN-5 L2).

Implements "global reachable-state fixpoint + conservative unsatisfiable-precondition detection"
as specified in L2_ADDENDUM.md.

Algorithm:
1. Seed initial state from all LogicVar defaults.  Entry quests = quests with no prerequisites
   and not referenced by any other quest's unlocks/preconditions.
2. Fixpoint: iterate until stable:
   - A quest is "reachable" when all its prerequisites are reachable AND its quest-level
     precondition is satisfiable under the current reachable-values set (conservative:
     only report unreachable when we can PROVE the precondition is permanently false).
   - On making a quest reachable: propagate effects (set/inc/dec on LogicVars and
     quest:<id>.done flags) into the reachable-values set; also follow unlocks edges.
3. Report: for each quest still not reachable, report QUEST_GLOBAL_UNREACHABLE only when
   we can identify a specific atomic condition that is permanently unsatisfiable (sound
   over-approximation: prefer to miss a report than to emit a false positive).

Non-overlap proof (written as assertions in tests):
- vs logic/audit.py QuestLogicRule: that checks single-quest internal stage graph + variable
  type errors.  This rule checks cross-quest global state propagation.
- vs lore_rules.py PrerequisiteCycleRule: that checks for prerequisite cycles (A->B->A).
  An unreachable quest need not be in a cycle; a cyclic quest need not be globally unreachable.
- vs reference_rules.py MissingPrerequisiteRule: that checks referenced ids exist.
  This rule assumes all referenced ids exist (they are already checked by the reference rule).

Conservatism (hard red-line: dry world produces zero false positives):
- Only report when we can positively prove a precondition atom is permanently unsatisfiable.
- inc/dec numeric variables are conservatively treated as "can reach any value" once modified.
- Complex boolean expressions (and/or/not) are only flagged when ALL paths are permanently false.
- Parse errors in preconditions: skip (do not report; might be reachable).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity


class QuestGlobalReachabilityRule:
    code = "QUEST_GLOBAL_UNREACHABLE"
    severity = Severity.WARNING
    category = Category.LOGIC

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        if not ctx.bundle.quests:
            return

        reachable_quest_ids, reachable_values = _compute_reachable_state(ctx)
        all_quest_ids = set(ctx.bundle.quests.keys())
        unreachable = all_quest_ids - reachable_quest_ids

        for quest_id in sorted(unreachable):
            # Try to identify which atomic condition makes the quest permanently unreachable.
            blocking_atom = _find_blocking_atom(
                quest_id, ctx, reachable_quest_ids, reachable_values
            )

            if blocking_atom is None:
                # We cannot identify a specific unsatisfiable atom — conservatively skip.
                # This prevents false positives (e.g. when the quest is unreachable due to
                # graph structure but we cannot prove a specific atom is permanently false).
                continue

            yield Issue(
                rule_code=self.code,
                severity=self.severity,
                category=self.category,
                target_ref=f"quest:{quest_id}",
                message=(
                    f"Quest '{quest_id}' cannot be reached: {blocking_atom}. "
                    "Check prerequisites/unlocks/precondition for missing links."
                ),
                evidence=[
                    Evidence(
                        kind="quest_reachability",
                        data={
                            "quest_id": quest_id,
                            "blocking_atom": blocking_atom,
                            "reachable_quest_count": len(reachable_quest_ids),
                        },
                    )
                ],
            )


# ---------------------------------------------------------------------------
# Reachable-state fixpoint computation
# ---------------------------------------------------------------------------

Value = bool | int | str

# Sentinel for "this numeric variable has been modified (inc/dec) and could be any value"
_MULTI = "__multi__"


class _ReachableValues:
    """Tracks which values each variable can have in reachable states.

    - Bool/enum variables: a set of concrete reachable values.
    - Numeric (int) variables after inc/dec: flagged as _MULTI (conservatively, any value).
    - quest:<id>.done flags: stored as booleans (initially False, set to True when reachable).
    """

    def __init__(self) -> None:
        # var_id -> set of values | _MULTI (for numeric-after-mutation)
        self._vals: dict[str, set | str] = {}

    def seed_var(self, var_id: str, default: Value) -> None:
        if var_id not in self._vals:
            if isinstance(default, bool):
                self._vals[var_id] = {default}
            elif isinstance(default, int):
                self._vals[var_id] = {default}
            else:
                self._vals[var_id] = {default}

    def add_value(self, var_id: str, value: Value) -> bool:
        """Add a concrete value; return True if state changed."""
        current = self._vals.get(var_id)
        if current == _MULTI:
            return False
        if current is None:
            self._vals[var_id] = {value}
            return True
        assert isinstance(current, set)
        if value in current:
            return False
        current.add(value)
        return True

    def mark_multi(self, var_id: str) -> bool:
        """Mark numeric var as potentially any value (inc/dec path). Returns True if changed."""
        if self._vals.get(var_id) == _MULTI:
            return False
        self._vals[var_id] = _MULTI
        return True

    def mark_quest_done(self, quest_id: str) -> bool:
        """Mark quest:<id>.done as True in reachable state. Return True if changed."""
        key = f"quest:{quest_id}.done"
        current = self._vals.get(key)
        if current == _MULTI:
            return False
        if isinstance(current, set) and True in current:
            return False
        if current is None:
            self._vals[key] = {True}
        else:
            assert isinstance(current, set)
            current.add(True)
        return True

    def can_be_true(self, ref_name: str) -> bool:
        """Can ref_name evaluate to True in some reachable state?"""
        current = self._vals.get(ref_name)
        if current is None:
            # Unknown variable: default False; if it's a quest ref, assume reachable unknown = False
            return False
        if current == _MULTI:
            return True  # numeric: conservatively could be any value
        return True in current

    def can_satisfy_eq(self, ref_name: str, target_val: Value) -> bool:
        """Can ref_name == target_val in some reachable state?"""
        current = self._vals.get(ref_name)
        if current is None:
            return False
        if current == _MULTI:
            return True  # conservative: could match anything
        return target_val in current

    def is_var_known(self, var_id: str) -> bool:
        return var_id in self._vals


def _apply_effects_to_reachable(
    effects: list[Any],  # list[Effect]
    rv: _ReachableValues,
) -> bool:
    """Apply a list of Effect objects into the reachable-values set. Return True if changed."""
    changed = False
    for eff in effects:
        if eff.op == "set":
            if rv.add_value(eff.var, eff.value):
                changed = True
        elif eff.op in ("inc", "dec"):
            if rv.mark_multi(eff.var):
                changed = True
    return changed


def _compute_reachable_state(
    ctx: AuditContext,
) -> tuple[set[str], _ReachableValues]:
    """Fixpoint: propagate reachable state across quests.

    Returns (reachable_quest_ids, reachable_values).
    """
    rv = _ReachableValues()

    # Seed all logic variables from their defaults
    for quest in ctx.bundle.quests.values():
        if quest.logic is not None:
            for var in quest.logic.variables:
                rv.seed_var(var.id, var.default)

    # Seed all quest:<id>.done as initially False
    for quest_id in ctx.bundle.quests:
        rv.seed_var(f"quest:{quest_id}.done", False)

    reachable: set[str] = set()

    # Identify prerequisite-free quests (candidates for entry quests)
    # An entry quest is one that has empty prerequisites AND that is not "blocked by
    # its own precondition being permanently false given the initial state".
    # We first compute which quests have no prerequisite edges (from any source).
    # We do this inside the fixpoint: in iteration 0 we consider quests with no prereqs;
    # on later iterations, quests whose prereqs are all now reachable.

    changed = True
    while changed:
        changed = False
        for quest_id, quest in ctx.bundle.quests.items():
            if quest_id in reachable:
                continue
            # Check prerequisites: all must be reachable
            if quest.prerequisites and not all(p in reachable for p in quest.prerequisites):
                continue
            # Check quest-level precondition: must be satisfiable given reachable_values
            if quest.logic is not None and quest.logic.precondition.strip():
                if not _precondition_satisfiable(quest.logic.precondition, rv):
                    continue
            # Quest is now reachable: mark it and propagate effects
            reachable.add(quest_id)
            changed = True
            # Propagate: mark quest done flag
            rv.mark_quest_done(quest_id)
            # Propagate: apply stage effects and branch effects
            if quest.logic is not None:
                for stage_logic in quest.logic.stage_logic:
                    if _apply_effects_to_reachable(stage_logic.effects_on_complete, rv):
                        pass  # inner changed; outer will re-check
                for branch in quest.logic.branches:
                    if _apply_effects_to_reachable(branch.effects, rv):
                        pass
                # Propagate unlocks: unlocked quests may now have their prereqs met
                # (they are just quest ids; the fixpoint loop will pick them up)

    return reachable, rv


def _precondition_satisfiable(precondition: str, rv: _ReachableValues) -> bool:
    """Return True if the precondition MIGHT be satisfiable in some reachable state.

    Conservative: returns True (satisfiable) when we cannot prove it is permanently false.
    This prevents false positives at the cost of missing some true positives.
    """
    try:
        from ...logic.expr import parse_expr
        expr = parse_expr(precondition)
    except Exception:
        return True  # parse error: assume satisfiable (conservative)

    return _expr_can_be_true(expr, rv)


def _expr_can_be_true(expr: Any, rv: _ReachableValues) -> bool:
    """Return True if expr can evaluate to True under some reachable state assignment."""
    from ...logic.expr import BoolLit, BoolOp, Compare, Not, Ref

    if isinstance(expr, BoolLit):
        return expr.value  # True literal: always satisfiable; False literal: never

    if isinstance(expr, Ref):
        # For a boolean ref: can it be True?
        return rv.can_be_true(expr.name)

    if isinstance(expr, Not):
        # not(X) can be true iff X can be False
        return _expr_can_be_false(expr.operand, rv)

    if isinstance(expr, BoolOp):
        if expr.op == "and":
            # (A and B) can be true iff both A and B can be true simultaneously
            # Conservative: check each independently (may over-approve conjunctions)
            return _expr_can_be_true(expr.left, rv) and _expr_can_be_true(expr.right, rv)
        else:  # "or"
            # (A or B) can be true iff at least one can be true
            return _expr_can_be_true(expr.left, rv) or _expr_can_be_true(expr.right, rv)

    if isinstance(expr, Compare):
        if expr.op == "==":
            from ...logic.expr import BoolLit as BL
            from ...logic.expr import IntLit, StrLit
            from ...logic.expr import Ref as ExprRef
            if isinstance(expr.left, ExprRef) and isinstance(expr.right, (BL, IntLit, StrLit)):
                return rv.can_satisfy_eq(expr.left.name, expr.right.value)
            if isinstance(expr.right, ExprRef) and isinstance(expr.left, (BL, IntLit, StrLit)):
                return rv.can_satisfy_eq(expr.right.name, expr.left.value)
        # For other comparisons (!=, <, >, etc.) be conservative: assume satisfiable
        return True

    # Unknown node type: conservative
    return True


def _expr_can_be_false(expr: Any, rv: _ReachableValues) -> bool:
    """Return True if expr can evaluate to False under some reachable state."""
    from ...logic.expr import BoolLit, BoolOp, Not, Ref

    if isinstance(expr, BoolLit):
        return not expr.value

    if isinstance(expr, Ref):
        # bool ref can be False if False is in reachable values, or if it's unknown
        current = rv._vals.get(expr.name)
        if current is None:
            return True  # unknown: conservative
        if current == _MULTI:
            return True  # multi-value: assume can be false too
        return False in current

    if isinstance(expr, Not):
        return _expr_can_be_true(expr.operand, rv)

    if isinstance(expr, BoolOp):
        if expr.op == "and":
            return _expr_can_be_false(expr.left, rv) or _expr_can_be_false(expr.right, rv)
        else:  # "or"
            return _expr_can_be_false(expr.left, rv) and _expr_can_be_false(expr.right, rv)

    return True  # conservative


# ---------------------------------------------------------------------------
# Find a specific blocking atom for reporting
# ---------------------------------------------------------------------------

def _find_blocking_atom(
    quest_id: str,
    ctx: AuditContext,
    reachable_quest_ids: set[str],
    rv: _ReachableValues,
) -> str | None:
    """Return a human-readable explanation of why quest_id is unreachable, or None if
    we cannot identify a specific permanently-false atom (conservative: no report when unsure).
    """
    quest = ctx.bundle.quests[quest_id]

    # Check missing prerequisites
    for prereq in quest.prerequisites:
        if prereq not in reachable_quest_ids:
            return f"prerequisite '{prereq}' is not globally reachable"

    # Check quest-level precondition atoms
    if quest.logic is not None and quest.logic.precondition.strip():
        atom_desc = _find_permanently_false_atom(quest.logic.precondition, rv)
        if atom_desc is not None:
            return f"precondition atom permanently false: {atom_desc}"

    # Cannot identify a specific atom
    return None


def _find_permanently_false_atom(precondition: str, rv: _ReachableValues) -> str | None:
    """Return a description of a specific atom that is permanently false, or None."""
    try:
        from ...logic.expr import parse_expr
        expr = parse_expr(precondition)
    except Exception:
        return None

    return _find_false_atom_in_expr(expr, rv)


def _find_false_atom_in_expr(expr: Any, rv: _ReachableValues) -> str | None:
    """Recursively find a permanently-false atomic condition. Return description or None."""
    from ...logic.expr import BoolLit, BoolOp, Compare, IntLit, Not, Ref, StrLit

    if isinstance(expr, BoolLit):
        return "literal false" if not expr.value else None

    if isinstance(expr, Ref):
        # A boolean ref that can never be True
        if not rv.can_be_true(expr.name):
            return f"'{expr.name}' can never be true in reachable states"
        return None

    if isinstance(expr, Not):
        # not(X): permanently false when X can never be False
        if not _expr_can_be_false(expr.operand, rv):
            return f"'{expr}' is permanently true (inner cannot be false)"
        return None

    if isinstance(expr, Compare):
        if expr.op == "==":
            if isinstance(expr.left, Ref) and isinstance(expr.right, (BoolLit, IntLit, StrLit)):
                if not rv.can_satisfy_eq(expr.left.name, expr.right.value):
                    return (
                        f"'{expr.left.name} == {expr.right.value!r}' can never be satisfied"
                    )
            elif isinstance(expr.right, Ref) and isinstance(expr.left, (BoolLit, IntLit, StrLit)):
                if not rv.can_satisfy_eq(expr.right.name, expr.left.value):
                    return (
                        f"'{expr.right.name} == {expr.left.value!r}' can never be satisfied"
                    )
        return None

    if isinstance(expr, BoolOp):
        if expr.op == "and":
            # For (A and B): find the first atom that is permanently false
            left_atom = _find_false_atom_in_expr(expr.left, rv)
            if left_atom:
                return left_atom
            return _find_false_atom_in_expr(expr.right, rv)
        else:  # "or"
            # Both arms must be permanently false for us to report
            left_atom = _find_false_atom_in_expr(expr.left, rv)
            right_atom = _find_false_atom_in_expr(expr.right, rv)
            if left_atom and right_atom:
                return f"both branches false: ({left_atom}) and ({right_atom})"
            return None

    return None
