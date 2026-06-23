"""A small, safe boolean expression language for quest logic — tokenize + recursive-descent parse
into an AST. NO ``eval``/``exec``: expressions are data, evaluated explicitly (see semantics.py), so
a malformed or adversarial expression can only ever raise ``LogicSyntaxError``, never execute.

Grammar (lowest to highest precedence):
    or   := and ('or' and)*
    and  := not ('and' not)*
    not  := 'not' not | compare
    cmp  := atom (('=='|'!='|'<'|'>'|'<='|'>=') atom)?
    atom := '(' or ')' | 'true' | 'false' | INT | STRING | REF
REF is an identifier like ``flag_x`` or a state ref like ``quest:q1.done`` (':'/'.' allowed).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


class LogicSyntaxError(ValueError):
    """Raised when an expression cannot be parsed. Never raised from evaluation of a parsed tree."""


@dataclass(frozen=True)
class BoolLit:
    value: bool


@dataclass(frozen=True)
class IntLit:
    value: int


@dataclass(frozen=True)
class StrLit:
    value: str


@dataclass(frozen=True)
class Ref:
    name: str


@dataclass(frozen=True)
class Not:
    operand: Expr


@dataclass(frozen=True)
class BoolOp:
    op: str  # "and" | "or"
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Compare:
    op: str  # "==" "!=" "<" ">" "<=" ">="
    left: Expr
    right: Expr


Expr = BoolLit | IntLit | StrLit | Ref | Not | BoolOp | Compare

_KEYWORDS = {"and", "or", "not", "true", "false"}
_COMPARE = {"==", "!=", "<", ">", "<=", ">="}
_MAX_LEN = 2000  # no legitimate logic expression is longer; reject pathological input early
_MAX_DEPTH = 64  # cap parenthesis nesting so deep input cannot cause a RecursionError
_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_:.]*\Z")
_TOKEN = re.compile(r"==|!=|<=|>=|[<>()]|\d+|'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_:.]*")


def _tokenize(src: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    n = len(src)
    while pos < n:
        if src[pos].isspace():
            pos += 1
            continue
        match = _TOKEN.match(src, pos)
        if match is None:
            raise LogicSyntaxError(f"unexpected character {src[pos]!r} at position {pos}")
        tokens.append(match.group(0))
        pos = match.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.toks = tokens
        self.i = 0
        self._depth = 0

    def _peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self) -> str:
        token = self._peek()
        if token is None:
            raise LogicSyntaxError("unexpected end of expression")
        self.i += 1
        return token

    def parse(self) -> Expr:
        expr = self._parse_or()
        if self._peek() is not None:
            raise LogicSyntaxError(f"unexpected trailing token {self._peek()!r}")
        return expr

    def _parse_or(self) -> Expr:
        left = self._parse_and()
        while self._peek() == "or":
            self._next()
            left = BoolOp("or", left, self._parse_and())
        return left

    def _parse_and(self) -> Expr:
        left = self._parse_not()
        while self._peek() == "and":
            self._next()
            left = BoolOp("and", left, self._parse_not())
        return left

    def _parse_not(self) -> Expr:
        if self._peek() == "not":
            self._next()
            return Not(self._parse_not())
        return self._parse_compare()

    def _parse_compare(self) -> Expr:
        left = self._parse_atom()
        if self._peek() in _COMPARE:
            op = self._next()
            return Compare(op, left, self._parse_atom())
        return left

    def _parse_atom(self) -> Expr:
        token = self._next()
        if token == "(":
            self._depth += 1
            if self._depth > _MAX_DEPTH:
                raise LogicSyntaxError("expression nesting too deep")
            inner = self._parse_or()
            closing = self._peek()
            if closing != ")":
                raise LogicSyntaxError("missing closing ')'")
            self._next()
            self._depth -= 1
            return inner
        if token == "true":
            return BoolLit(True)
        if token == "false":
            return BoolLit(False)
        if token[0] in "'\"":
            return StrLit(token[1:-1])
        if token.isdigit():
            return IntLit(int(token))
        if token in _KEYWORDS:
            raise LogicSyntaxError(f"unexpected keyword {token!r}")
        if _REF.match(token):
            return Ref(token)
        raise LogicSyntaxError(f"unexpected token {token!r}")


def parse_expr(src: str) -> Expr:
    """Parse an expression source string into an AST, or raise ``LogicSyntaxError``. Callers treat
    an empty string as 'no expression' (always true) and must not call this on it."""
    if len(src) > _MAX_LEN:
        raise LogicSyntaxError("expression too long")
    return _Parser(_tokenize(src)).parse()


def refs_in(expr: Expr) -> set[str]:
    """Every variable/state reference an expression reads (for symbol-table building)."""
    if isinstance(expr, Ref):
        return {expr.name}
    if isinstance(expr, Not):
        return refs_in(expr.operand)
    if isinstance(expr, (BoolOp, Compare)):
        return refs_in(expr.left) | refs_in(expr.right)
    return set()


def render_expr(expr: Expr, ref: Callable[[str], str] = lambda name: name) -> str:
    """Render an AST back to an infix string, mapping reference names through ``ref`` (a hook for
    callers that need to sanitize ``quest:q.done`` into valid target identifiers)."""
    if isinstance(expr, BoolLit):
        return "true" if expr.value else "false"
    if isinstance(expr, IntLit):
        return str(expr.value)
    if isinstance(expr, StrLit):
        return f'"{expr.value}"'
    if isinstance(expr, Ref):
        return ref(expr.name)
    if isinstance(expr, Not):
        return f"not ({render_expr(expr.operand, ref)})"
    if isinstance(expr, BoolOp):
        return f"({render_expr(expr.left, ref)} {expr.op} {render_expr(expr.right, ref)})"
    return f"{render_expr(expr.left, ref)} {expr.op} {render_expr(expr.right, ref)}"
