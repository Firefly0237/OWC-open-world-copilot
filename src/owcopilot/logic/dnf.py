# 条件表达式 -> DNF（析取范式）编译器：把自研表达式 AST 编译成引擎侧可直接消费的
# {var, op, value} 原子子句数组，运行时侧（UE Blueprint）零解析器。
"""Compile a quest-logic condition expression into DNF (disjunctive normal form) — an OR of AND
groups, each an atomic ``{var, op, value}`` clause — so a consumer with no expression parser (a UE
Blueprint has neither a recursive-descent parser nor a JSON-string evaluator) can gate on it with
only comparison + boolean AND/OR.

This is a SEPARATE pass over the existing ``logic/expr.py`` AST; the parser/grammar in ``expr.py``
is untouched. An expression that cannot be reduced to this atom vocabulary — a comparison between
two variables (``a == b``), a comparison between two literals, or one that blows past the
group/atom caps below — is reported as **not compilable** rather than guessed at: the caller keeps
the original source string as the fallback (see ``DnfResult.compilable``).

Correctness is verified by a differential test (``tests/domain/test_logic_dnf.py``): every
compilable expression that appears in the existing test suite's fixtures is checked against the
production evaluator (``logic/semantics.evaluate``) across the full combination of test values
for its variables — bool variables exhaustively, int variables at their comparison boundaries.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .expr import (
    BoolLit,
    BoolOp,
    Compare,
    Expr,
    IntLit,
    LogicSyntaxError,
    Not,
    Ref,
    StrLit,
    parse_expr,
)

Value = bool | int | str

# expr.py's Compare.op set, exactly — the atom vocabulary supports no operators beyond these.
_NEGATE_OP = {"==": "!=", "!=": "==", "<": ">=", ">": "<=", "<=": ">", ">=": "<"}
_FLIP_OP = {"==": "==", "!=": "!=", "<": ">", ">": "<", "<=": ">=", ">=": "<="}

# Bounds so a pathologically nested (but syntactically valid — expr.py itself caps parenthesis
# depth at 64) expression cannot blow up cross-product distribution: exceeding either turns the
# WHOLE expression not-compilable rather than hanging or exhausting memory. No real quest/dialogue
# condition comes close to these.
_MAX_GROUPS = 256
_MAX_ATOMS_PER_GROUP = 32


class _NotCompilable(Exception):
    """Internal control-flow signal for ``_compile`` — never escapes ``compile_condition``."""


@dataclass(frozen=True)
class Atom:
    """One atomic clause: ``var OP value`` (e.g. ``gold >= 5``, or ``helped == true`` for a bare
    boolean ref / ``helped == false`` for its negation)."""

    var: str
    op: str  # "==" | "!=" | "<" | ">" | "<=" | ">="
    value: Value


@dataclass(frozen=True)
class DnfResult:
    """The compiled form of one condition source string.

    ``groups`` is an OR of AND-groups (``groups[i]`` is a tuple of ``Atom`` all of which must hold
    for that group to be satisfied); the whole condition holds when ANY group holds — this is the
    reference semantics ``evaluate_dnf`` implements. When ``compilable`` is False, ``groups`` is
    always empty and ``source`` is the only usable fallback (render the raw expression, or refuse
    to gate and let a human review it).
    """

    source: str
    compilable: bool
    groups: tuple[tuple[Atom, ...], ...] = ()

    @property
    def single_and_group(self) -> tuple[Atom, ...] | None:
        """The flat AND-only clause list a Blueprint can consume directly (a plain array, no OR
        logic needed) — only when compilation succeeded AND no OR was needed (exactly one group,
        by far the most common real case: preconditions/conditions are almost always a plain
        conjunction). ``None`` otherwise; the caller falls back to the raw source string and a
        human reviews it (the demo content is authored to stay within this flat AND subset)."""
        if self.compilable and len(self.groups) == 1:
            return self.groups[0]
        return None


def compile_condition(source: str) -> DnfResult:
    """Compile a condition source string to DNF. Never raises: an empty/blank source (no gate) is
    trivially compilable (one empty AND-group = always true, matching how the rest of the codebase
    already treats it — see ``simulate.py``'s ``_truthy``); one that fails to parse, or that uses a
    form outside the supported atom vocabulary, comes back with ``compilable=False`` and the source
    preserved verbatim so a caller can still show/log the original expression."""
    if not source.strip():
        return DnfResult(source=source, compilable=True, groups=((),))
    try:
        expr = parse_expr(source)
        groups = _compile(expr, negate=False)
    except (LogicSyntaxError, _NotCompilable):
        return DnfResult(source=source, compilable=False)
    return DnfResult(source=source, compilable=True, groups=tuple(tuple(g) for g in groups))


def _compile(expr: Expr, *, negate: bool) -> list[list[Atom]]:
    """Return the DNF groups for ``expr`` (or its negation when ``negate``), as an OR of AND-lists
    of ``Atom``. Raises ``_NotCompilable`` for anything outside the supported vocabulary — callers
    never see a partially-wrong result, only a clean not-compilable outcome."""
    if isinstance(expr, BoolLit):
        holds = expr.value != negate
        return [[]] if holds else []
    if isinstance(expr, Not):
        return _compile(expr.operand, negate=not negate)
    if isinstance(expr, Ref):
        return [[Atom(expr.name, "==", not negate)]]
    if isinstance(expr, Compare):
        return [[_atom_from_compare(expr, negate=negate)]]
    if isinstance(expr, BoolOp):
        return _compile_bool_op(expr, negate=negate)
    # IntLit / StrLit used directly as a boolean (e.g. a bare literal condition) is not a
    # supported form — the grammar allows it syntactically, but it is meaningless as a gate.
    raise _NotCompilable()


def _compile_bool_op(expr: BoolOp, *, negate: bool) -> list[list[Atom]]:
    op = expr.op
    if negate:  # De Morgan: not(a and b) = not a or not b; not(a or b) = not a and not b
        op = "or" if op == "and" else "and"
    left = _compile(expr.left, negate=negate)
    right = _compile(expr.right, negate=negate)
    if op == "or":
        groups = left + right
    else:
        if len(left) * len(right) > _MAX_GROUPS:
            raise _NotCompilable()
        groups = []
        for left_group, right_group in itertools.product(left, right):
            if len(left_group) + len(right_group) > _MAX_ATOMS_PER_GROUP:
                raise _NotCompilable()
            groups.append(left_group + right_group)
    if len(groups) > _MAX_GROUPS:
        raise _NotCompilable()
    return groups


def _atom_from_compare(cmp: Compare, *, negate: bool) -> Atom:
    op = _NEGATE_OP[cmp.op] if negate else cmp.op
    left, right = cmp.left, cmp.right
    if isinstance(left, Ref) and isinstance(right, (BoolLit, IntLit, StrLit)):
        return Atom(left.name, op, right.value)
    if isinstance(right, Ref) and isinstance(left, (BoolLit, IntLit, StrLit)):
        return Atom(right.name, _FLIP_OP[op], left.value)
    # var-vs-var (``a == b``) or literal-vs-literal: there is no single variable to key the atom
    # on, so this comparison shape falls outside the {var, op, value} vocabulary entirely.
    raise _NotCompilable()


def evaluate_dnf(result: DnfResult, state: Mapping[str, Value]) -> bool:
    """Reference evaluator over a compiled ``DnfResult`` — OR of ANDs of ``var OP value``. This is
    what the differential test checks against ``logic.semantics.evaluate``; it is not on any
    production evaluation path (the simulator/audit still evaluate the parsed AST directly — this
    function exists to let the *exported* clause data be checked, standing in for what a Blueprint
    would compute)."""
    return any(all(_atom_holds(atom, state) for atom in group) for group in result.groups)


def _atom_holds(atom: Atom, state: Mapping[str, Value]) -> bool:
    if atom.var not in state:
        raise KeyError(f"reference not in state: {atom.var}")
    left, right = state[atom.var], atom.value
    if atom.op == "==":
        return left == right
    if atom.op == "!=":
        return left != right
    if not isinstance(left, int) or isinstance(left, bool):
        raise TypeError(f"'{atom.op}' needs an integer var, got {left!r}")
    if not isinstance(right, int) or isinstance(right, bool):
        raise TypeError(f"'{atom.op}' needs an integer value, got {right!r}")
    if atom.op == "<":
        return left < right
    if atom.op == ">":
        return left > right
    if atom.op == "<=":
        return left <= right
    return left >= right


def atoms_in(result: DnfResult) -> Iterable[Atom]:
    """Every atom appearing anywhere in the compiled groups (for building test/probe states)."""
    return (atom for group in result.groups for atom in group)
