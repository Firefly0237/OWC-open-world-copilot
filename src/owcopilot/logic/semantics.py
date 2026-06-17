"""Static type checking, evaluation, and state for the quest logic expression language.

Evaluation is explicit tree-walking over a parsed AST against a concrete state — there is no
``eval``. Type checking is a separate static pass that reports (not raises) so the auditor can
collect every problem at once.
"""

from __future__ import annotations

from collections.abc import Mapping

from .expr import BoolLit, BoolOp, Compare, Expr, IntLit, Not, Ref, StrLit

# ref -> declared type. quest-state refs (e.g. quest:q1.done) are treated as bool automatically.
Symbols = Mapping[str, str]
_ORDER_OPS = {"<", ">", "<=", ">="}
Value = bool | int | str


class LogicEvalError(ValueError):
    """Raised when a parsed expression cannot be evaluated against a state (e.g. unknown ref)."""


def _ref_type(name: str, symbols: Symbols) -> str | None:
    declared = symbols.get(name)
    if declared is not None:
        return declared
    if name.startswith("quest:") and name.endswith(".done"):
        return "bool"  # quest completion state is a boolean
    if name.startswith("rep:"):
        return "int"  # faction reputation/standing is integer state; the faction id behind the
        # `rep:` prefix is validated against the entity graph by the bundle-aware audit rule.
    return None


def type_errors(expr: Expr, symbols: Symbols) -> list[str]:
    """Report undefined references and type mismatches (does not raise)."""
    errors: list[str] = []

    def typ(node: Expr) -> str | None:
        if isinstance(node, BoolLit):
            return "bool"
        if isinstance(node, IntLit):
            return "int"
        if isinstance(node, StrLit):
            return "enum"
        if isinstance(node, Ref):
            kind = _ref_type(node.name, symbols)
            if kind is None:
                errors.append(f"undefined variable: {node.name}")
                return None
            return kind
        if isinstance(node, Not):
            inner = typ(node.operand)
            if inner not in (None, "bool"):
                errors.append(f"'not' needs a boolean, got {inner}")
            return "bool"
        if isinstance(node, BoolOp):
            for side in (node.left, node.right):
                side_type = typ(side)
                if side_type not in (None, "bool"):
                    errors.append(f"'{node.op}' needs booleans, got {side_type}")
            return "bool"
        if isinstance(node, Compare):
            left_type, right_type = typ(node.left), typ(node.right)
            if node.op in _ORDER_OPS:
                if left_type not in (None, "int") or right_type not in (None, "int"):
                    errors.append(
                        f"'{node.op}' needs integer operands, got {left_type} and {right_type}"
                    )
            elif left_type is not None and right_type is not None and left_type != right_type:
                errors.append(f"cannot compare {left_type} to {right_type}")
            return "bool"
        return None

    typ(expr)
    return errors


def _as_bool(value: Value) -> bool:
    if not isinstance(value, bool):
        raise LogicEvalError(f"expected a boolean, got {value!r}")
    return value


def evaluate(expr: Expr, state: Mapping[str, Value]) -> Value:
    """Evaluate a parsed expression against a concrete state. Assumes it type-checked; an unknown
    reference raises ``LogicEvalError`` rather than guessing."""
    if isinstance(expr, BoolLit):
        return expr.value
    if isinstance(expr, IntLit):
        return expr.value
    if isinstance(expr, StrLit):
        return expr.value
    if isinstance(expr, Ref):
        if expr.name not in state:
            raise LogicEvalError(f"reference not in state: {expr.name}")
        return state[expr.name]
    if isinstance(expr, Not):
        return not _as_bool(evaluate(expr.operand, state))
    if isinstance(expr, BoolOp):
        left = _as_bool(evaluate(expr.left, state))
        if expr.op == "and":
            return left and _as_bool(evaluate(expr.right, state))
        return left or _as_bool(evaluate(expr.right, state))
    if isinstance(expr, Compare):
        return _compare(expr.op, evaluate(expr.left, state), evaluate(expr.right, state))
    raise LogicEvalError("unknown expression node")


def _compare(op: str, left: Value, right: Value) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if not isinstance(left, int) or isinstance(left, bool):
        raise LogicEvalError(f"'{op}' needs integers, got {left!r}")
    if not isinstance(right, int) or isinstance(right, bool):
        raise LogicEvalError(f"'{op}' needs integers, got {right!r}")
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    return left >= right


class WorldState:
    """A mutable snapshot of logic variable values, for simulation/playtest (reused by WS-E)."""

    def __init__(self, values: Mapping[str, Value] | None = None) -> None:
        self.values: dict[str, Value] = dict(values or {})

    def as_mapping(self) -> dict[str, Value]:
        return dict(self.values)

    def apply(self, var: str, op: str, value: Value) -> None:
        if op == "set":
            self.values[var] = value
            return
        current = self.values.get(var, 0)
        delta = int(value) if not isinstance(value, bool) else int(value)
        base = current if isinstance(current, int) and not isinstance(current, bool) else 0
        self.values[var] = base + delta if op == "inc" else base - delta
