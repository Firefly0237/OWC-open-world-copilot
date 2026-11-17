"""B1 · DNF condition compiler: unit coverage + the differential test against the production
evaluator.

The differential test is the load-bearing part: it does not hand-pick
a handful of example expressions — it mechanically harvests every ``condition=``/``precondition=``
string literal that appears anywhere in the existing test suite (a small AST walk over every
``tests/**/test_*.py`` file), then for each one that compiles, checks the compiled DNF against
``logic.semantics.evaluate`` (the same evaluator the simulator/audit/dialogue-walker use) across
every combination of test values for its variables — bool variables exhaustively (both True/False),
int variables at their comparison boundaries. Any expression the codebase does not currently use to
gate anything is out of scope by construction (there is nothing to be differential against).
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from owcopilot.logic import evaluate, parse_expr
from owcopilot.logic.dnf import Atom, Value, atoms_in, compile_condition, evaluate_dnf

_TESTS_DIR = Path(__file__).resolve().parents[1]
_CONDITION_KWARGS = {"condition", "precondition"}


# --- harvesting ----------------------------------------------------------------------------


def _harvest_condition_literals() -> set[str]:
    """Every string literal passed as ``condition=``/``precondition=`` anywhere under
    ``tests/**/test_*.py`` — a mechanical sweep, not a hand-copied list, so it can
    never silently drop a fixture and automatically covers whatever gets added later."""
    literals: set[str] = set()
    for path in sorted(_TESTS_DIR.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg not in _CONDITION_KWARGS:
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                literals.add(node.value.value)
    return literals


def test_harvest_finds_a_healthy_number_of_fixture_expressions() -> None:
    """Sanity check on the harvester itself: if this drops to (near) zero, the AST walk broke, not
    that the fixtures disappeared — fail loudly rather than silently testing nothing."""
    assert len(_harvest_condition_literals()) >= 30


# --- candidate value construction for the differential sweep --------------------------------


def _candidates_for(var: str, atoms: list[Atom]) -> list[Value]:
    literal_values = [a.value for a in atoms if a.var == var]
    if not literal_values or all(isinstance(v, bool) for v in literal_values):
        return [True, False]  # bare/negated bool ref, or explicit bool comparison: exhaustive
    if all(isinstance(v, int) and not isinstance(v, bool) for v in literal_values):
        bounds: set[int] = {0, 1}
        for v in literal_values:
            bounds.update({v - 1, v, v + 1})
        return sorted(bounds)
    if all(isinstance(v, str) for v in literal_values):
        # every literal seen, plus one value guaranteed to differ from all of them
        probe = "__owc_probe_other__"
        while probe in literal_values:
            probe += "_"
        return [*dict.fromkeys(literal_values), probe]
    # a var compared against mixed literal types across different clauses — not a shape any
    # harvested fixture actually has, but stay correct rather than assume: test every literal seen.
    return list(dict.fromkeys(literal_values))


def _states_for(source: str) -> list[dict[str, Value]]:
    if not source.strip():
        return [{}]  # parse_expr() refuses "" ("no expression") — nothing to vary either way
    parsed = parse_expr(source)
    from owcopilot.logic import refs_in

    result = compile_condition(source)
    atoms = list(atoms_in(result))
    variables = sorted(refs_in(parsed))
    assert {a.var for a in atoms} == set(variables), (
        f"compiled atoms reference a different variable set than the source expression: {source!r}"
    )
    candidates = {v: _candidates_for(v, atoms) for v in variables}
    combos = list(itertools.product(*(candidates[v] for v in variables)))
    return [dict(zip(variables, combo, strict=True)) for combo in combos]


# --- the differential test itself ------------------------------------------------------------


def test_compiled_dnf_matches_production_evaluator_on_every_fixture_condition() -> None:
    sources = sorted(_harvest_condition_literals())
    checked_expressions = 0
    checked_states = 0
    not_compilable = 0

    for source in sources:
        result = compile_condition(source)
        if not result.compilable:
            not_compilable += 1
            continue
        checked_expressions += 1
        for state in _states_for(source):
            # empty source: the rest of the codebase (simulate.py's _truthy, dialogue.py's _seed)
            # never calls the parser on "" — it is a convention meaning "always true" — so mirror
            # that instead of calling parse_expr("") (which explicitly refuses empty input).
            original = True if not source.strip() else evaluate(parse_expr(source), state)
            compiled = evaluate_dnf(result, state)
            assert compiled == original, (
                f"DNF diverged from production evaluator: source={source!r} state={state!r} "
                f"original={original!r} compiled={compiled!r} groups={result.groups!r}"
            )
            checked_states += 1

    # both counters must be non-trivial: a harvester or compiler regression that made everything
    # "not compilable" (or made compile_condition a no-op) would otherwise pass vacuously.
    assert checked_expressions >= 15, checked_expressions
    assert checked_states >= 30, checked_states
    assert not_compilable >= 1  # the suite deliberately includes var-vs-var / parse-error cases


# --- unit coverage of the compiler's shape decisions -----------------------------------------


def test_bare_ref_and_negation() -> None:
    assert compile_condition("helped").groups == ((Atom("helped", "==", True),),)
    assert compile_condition("not helped").groups == ((Atom("helped", "==", False),),)


def test_and_produces_one_group_with_all_atoms() -> None:
    result = compile_condition("helped and gold >= 5")
    assert result.single_and_group == (
        Atom("helped", "==", True),
        Atom("gold", ">=", 5),
    )


def test_or_produces_multiple_groups_and_is_not_flat() -> None:
    result = compile_condition("a or b")
    assert result.compilable is True
    assert len(result.groups) == 2
    assert result.single_and_group is None  # OR needed -> not the flat BP contract


def test_de_morgan_negation_of_and_or() -> None:
    not_and = compile_condition("not (a and b)")
    assert set(not_and.groups) == {
        (Atom("a", "==", False),),
        (Atom("b", "==", False),),
    }
    not_or = compile_condition("not (a or b)")
    assert not_or.single_and_group == (Atom("a", "==", False), Atom("b", "==", False))


def test_double_negation_cancels() -> None:
    assert compile_condition("not (not helped)").single_and_group == (Atom("helped", "==", True),)


def test_literal_on_left_flips_operator() -> None:
    result = compile_condition("5 < gold")
    assert result.single_and_group == (Atom("gold", ">", 5),)


def test_negated_comparison_flips_operator() -> None:
    assert compile_condition("not (gold >= 5)").single_and_group == (Atom("gold", "<", 5),)


def test_empty_condition_is_vacuously_true() -> None:
    result = compile_condition("")
    assert result.compilable is True
    assert result.single_and_group == ()
    assert evaluate_dnf(result, {}) is True


@pytest.mark.parametrize(
    "source",
    [
        "a == b",  # var vs var: no single variable to key the atom on
        "5 == 6",  # literal vs literal
        "{{invalid syntax here}}",  # doesn't even parse
        "and or",
        "",  # handled separately (always compilable) — included here to prove no crash either way
    ],
)
def test_unsupported_or_broken_forms_never_raise(source: str) -> None:
    result = compile_condition(source)  # must not raise
    if source == "":
        assert result.compilable is True
    else:
        assert result.compilable is False
        assert result.source == source
        assert result.single_and_group is None


def test_long_flat_and_chain_still_compiles() -> None:
    # 20 chained ANDs of distinct bool refs (comfortably under the 32-atoms-per-group guard) stay
    # a single flat AND group regardless of length.
    source = " and ".join(f"v{i}" for i in range(20))
    result = compile_condition(source)
    assert result.compilable is True
    assert result.single_and_group is not None
    assert len(result.single_and_group) == 20


def test_or_explosion_past_group_cap_is_not_compilable() -> None:
    # 9 ANDed OR-pairs is 2**9 = 512 DNF groups after distribution — past the 256-group guard, so
    # this must degrade to "not compilable" rather than materializing 512 groups or hanging.
    pairs = " and ".join(f"(v{2 * i} or v{2 * i + 1})" for i in range(9))
    result = compile_condition(pairs)
    assert result.compilable is False
    assert result.source == pairs
