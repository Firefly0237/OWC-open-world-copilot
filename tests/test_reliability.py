"""Tests for the pass^k / pass@k reliability estimators (``evaluation/reliability.py``).

These pin the two canonical estimators to their published closed forms (τ-bench pass^k and
Chen-2021 pass@k), the cross-check invariants they must satisfy, the guided ValueErrors for
out-of-domain inputs, and the τ-bench mean-over-tasks aggregation. Tests assemble inputs and assert
outputs of the production functions — they do not re-derive the binomials.
"""

from __future__ import annotations

import math
from fractions import Fraction

import pytest

from owcopilot.assist.calibration import _wilson_interval
from owcopilot.evaluation.reliability import (
    aggregate_reliability,
    pass_at_k,
    pass_hat_k,
)


def test_pass_hat_k_matches_hand_values() -> None:
    assert pass_hat_k(4, 2, 2) == 1 / 6  # C(2,2)/C(4,2) = 1/6
    assert pass_hat_k(4, 4, 2) == 1.0  # all trials succeed
    assert pass_hat_k(4, 1, 2) == 0.0  # C(1,2)=0: fewer successes than the window
    assert pass_hat_k(5, 3, 1) == 0.6  # C(3,1)/C(5,1) = 3/5


def test_pass_at_k_matches_hand_values() -> None:
    assert pass_at_k(5, 1, 1) == 0.2  # 1 - C(4,1)/C(5,1) = 1 - 4/5
    assert pass_at_k(5, 2, 2) == 0.7  # 1 - C(3,2)/C(5,2) = 1 - 3/10
    assert pass_at_k(3, 2, 2) == 1.0  # n-c=1 < k=2: C(1,2)=0 -> 1.0 (edge)


def test_both_estimators_reduce_to_c_over_n_at_k_1() -> None:
    """pass@1 == pass^1 == c/n — the free cross-check from both papers."""
    for n in range(1, 8):
        for c in range(0, n + 1):
            assert pass_hat_k(n, c, 1) == c / n
            assert pass_at_k(n, c, 1) == c / n


def test_pass_at_k_dominates_pass_hat_k_over_validity_grid() -> None:
    """Optimistic (>=1 success) never scores below pessimistic (all-k success)."""
    for n in range(1, 8):
        for c in range(0, n + 1):
            for k in range(1, n + 1):
                assert pass_at_k(n, c, k) >= pass_hat_k(n, c, k)


@pytest.mark.parametrize("estimator", [pass_hat_k, pass_at_k])
@pytest.mark.parametrize(
    ("n", "c", "k"),
    [
        (2, 1, 3),  # k > n  (τ-bench forbids k > num_trials)
        (3, 4, 2),  # c > n
        (0, 0, 1),  # n < 1
        (3, 1, 0),  # k < 1
    ],
)
def test_estimators_raise_guided_valueerror_not_silent(estimator, n, c, k) -> None:
    """Out-of-domain inputs raise a clear ValueError — never ZeroDivisionError, never silent 0.0."""
    with pytest.raises(ValueError):
        estimator(n, c, k)


def test_aggregate_reliability_mean_over_tasks_matches_tau_bench() -> None:
    report = aggregate_reliability([(4, 2), (4, 4)])
    # τ-bench outer mean at k=2: (C(2,2)/C(4,2) + C(4,2)/C(4,2)) / 2 = (1/6 + 1)/2 = 0.5833
    assert report.pass_hat_k[2] == 0.5833
    # k=1 aggregate equals pooled success rate when n is uniform
    assert report.pass_hat_k[1] == report.success_rate == 0.75
    assert report.n_scenarios == 2
    assert report.max_trials == 4


def test_aggregate_reliability_ci_reuses_wilson_helper_not_a_copy() -> None:
    report = aggregate_reliability([(4, 2), (4, 4)])
    # pooled successes=6, trials=8 -> byte-equal to the calibration Wilson helper
    assert report.successes == 6
    assert report.trials == 8
    assert report.success_rate_ci == _wilson_interval(6, 8)


def test_aggregate_reliability_only_averages_eligible_scenarios() -> None:
    """A scenario with n < k must be excluded from that k, not counted as a 0."""
    report = aggregate_reliability([(2, 1), (4, 4)])
    # k=3 eligible only for the n=4 scenario: C(4,3)/C(4,3) = 1.0, mean over the single eligible one
    assert report.pass_hat_k[3] == 1.0
    assert report.max_trials == 4


def test_aggregate_reliability_single_trial_collapses_to_pass_at_1() -> None:
    """All n_trials=1 -> only k=1 defined, pass^1 == success rate, honest note says so."""
    report = aggregate_reliability([(1, 1), (1, 0), (1, 1)])
    assert set(report.pass_hat_k) == {1}
    assert report.pass_hat_k[1] == report.success_rate == round(2 / 3, 4)
    assert "n_trials=1" in report.note


def test_aggregate_reliability_rejects_impossible_trial() -> None:
    with pytest.raises(ValueError):
        aggregate_reliability([(2, 3)])  # c > n


def test_estimators_use_exact_integer_binomials() -> None:
    """Guard against float-ifying the binomials: the value is the correctly-rounded double of the
    exact rational built from math.comb, not an accumulated float subtraction."""
    assert pass_hat_k(9, 5, 3) == float(Fraction(math.comb(5, 3), math.comb(9, 3)))
    assert pass_at_k(9, 5, 3) == float(1 - Fraction(math.comb(4, 3), math.comb(9, 3)))
