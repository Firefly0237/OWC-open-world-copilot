# pass^k 可靠性度量：τ-bench 的悲观可靠性 + optimistic pass@k，均用精确二项式。
"""Reliability estimators for agent evaluation: τ-bench ``pass^k`` and optimistic ``pass@k``.

Two canonical, unbiased estimators over ``n`` independent trials of a task with ``c`` successes:

- ``pass_hat_k(n, c, k) = C(c, k) / C(n, k)`` — τ-bench's *pessimistic* reliability: the probability
  that **all** ``k`` sampled trials succeed. It decays toward 0 as ``k`` grows, exposing brittleness
  that single-shot ``pass@1`` hides (Yao et al., 2024, arXiv:2406.12045; reference implementation
  ``sierra-research/tau-bench`` ``run.py::display_metrics``).
- ``pass_at_k(n, c, k) = 1 - C(n-c, k) / C(n, k)`` — Chen et al. (2021) HumanEval Eq. (1): the
  *optimistic* probability that **at least one** of ``k`` trials passes (arXiv:2107.03374).

Both use exact integer binomials (``math.comb``), never a float approximation, and both reduce to
``c/n`` at ``k=1`` (``pass@1 == pass^1``). ``aggregate_reliability`` mirrors τ-bench's
mean-over-tasks aggregation, and reuses :func:`owcopilot.assist.calibration._wilson_interval` (not
a copy) for the pooled success-rate confidence interval. Pure and deterministic — no model calls.

Honesty note: the deterministic ``$0`` scorecard scenarios are single-trial (``n_trials=1``), so
``pass^k`` there honestly collapses to ``pass@1`` (only ``k=1`` is defined). Genuine ``k>1``
reliability needs repeated live sampling — the natural home for that is
``scripts/run_real_staged_world_seed.py`` (``--samples``), intentionally out of scope here. The
estimators themselves are exercised at ``n>1`` by the unit tests.

命名口径（W1-2 诚实标注，评估逻辑不变）：`evaluation/agent_eval.py::run_jd_dimension_scorecard`
里报出来的 ``pass_hat_k`` 数字，在当前唯一的调用点上就是 ``pass@1`` 换了个名字——因为所有场景
``n_trials=1``，``k`` 只在 ``k=1`` 处有定义。这不是命名错误（估计量本身 `pass_hat_k`/`pass_at_k`
的实现是对的、且在 ``n>1`` 下被单测覆盖过），而是**当前唯一的生产调用点从未喂过 n>1 的数据**。
在此仅做标注，不重命名函数（重命名会牵动 `ReliabilityReport`/多处测试引用），真正的 k>1 重复采样
是 Wave 5 W5-3（见 ``docs/研究/一轮修改优化_总实施方案.md``）。**在此之前，任何对外材料都不得把
这个数字描述为"k 次试验可靠性"，只能称其为 pass@1（=成功率）。**
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from fractions import Fraction

from pydantic import BaseModel

from ..assist.calibration import _wilson_interval


def _validate_trial(n: int, c: int) -> None:
    """Reject impossible (n, c) with a guided error instead of a silent wrong number."""
    if n < 1:
        raise ValueError(f"n (trials per task) must be >= 1, got {n}")
    if c < 0 or c > n:
        raise ValueError(f"c (successful trials) must be in [0, n]=[0, {n}], got {c}")


def _validate(n: int, c: int, k: int) -> None:
    """Full guard for a single (n, c, k) estimate. Raises a clear ValueError (never a
    ZeroDivisionError, never a silent 0.0) for out-of-domain inputs — in particular k > n, which
    τ-bench forbids (k ranges 1..num_trials, so k <= n always)."""
    _validate_trial(n, c)
    if k < 1:
        raise ValueError(f"k (sampled trials) must be >= 1, got {k}")
    if k > n:
        raise ValueError(
            f"k={k} exceeds n={n}: pass^k / pass@k are only defined for k <= n. "
            f"Run at least k trials per task before requesting a {k}-trial estimate."
        )


def pass_hat_k(n: int, c: int, k: int) -> float:
    """τ-bench pessimistic reliability: ``C(c, k) / C(n, k)`` — probability all ``k`` trials pass.

    Exact per-task term from ``tau-bench`` ``run.py::display_metrics``. ``math.comb(c, k)`` is 0
    when ``c < k`` (fewer successes than the window), so a task that cannot fill ``k`` scores 0.
    The ratio of the integer binomials is formed exactly (``Fraction``) and rounded to a float only
    once at the end — never a float approximation of the binomials.
    """
    _validate(n, c, k)
    return float(Fraction(math.comb(c, k), math.comb(n, k)))


def pass_at_k(n: int, c: int, k: int) -> float:
    """Chen et al. (2021) optimistic estimator: ``1 - C(n-c, k) / C(n, k)`` — probability at least
    one of ``k`` trials passes. ``math.comb(n-c, k)`` is 0 when ``n-c < k`` (too few failures to
    fill the window), yielding 1.0. The optimistic counterpart to :func:`pass_hat_k`.

    Module-level API for scripts / future repeated-sampling paths; no production caller today
    (the reliability report is built on the pessimistic ``pass_hat_k``), so it is deliberately
    not re-exported from ``owcopilot.evaluation``.

    The whole ``1 - C(n-c,k)/C(n,k)`` is evaluated in exact rational arithmetic and converted to a
    float once (equivalent to the paper's numerically-stable product form) so it returns the
    correctly-rounded double — ``float(1.0 - c/n)`` would leak subtraction rounding (0.2 -> 0.199…).
    """
    _validate(n, c, k)
    return float(1 - Fraction(math.comb(n - c, k), math.comb(n, k)))


class ReliabilityReport(BaseModel):
    """Aggregated reliability over a set of scenarios, τ-bench mean-over-tasks style."""

    n_scenarios: int
    successes: int  # pooled successful trials across all scenarios
    trials: int  # pooled trials across all scenarios
    success_rate: float  # pooled successes / trials (== pass_hat_k[1] when n is uniform)
    success_rate_ci: list[float] | None  # 95% Wilson interval over the pooled counts
    max_trials: int  # largest n across scenarios — the highest k with any eligible scenario
    pass_hat_k: dict[int, float]  # k -> mean pass^k over scenarios eligible at that k (n >= k)
    note: str


def aggregate_reliability(
    trials: Sequence[tuple[int, int]],
    *,
    context: str = "",
) -> ReliabilityReport:
    """Aggregate per-scenario ``(n_trials, successes)`` into a :class:`ReliabilityReport`.

    ``pass_hat_k[k]`` is the arithmetic mean of :func:`pass_hat_k` over the scenarios *eligible* at
    ``k`` (those with ``n_trials >= k``) — exactly τ-bench's ``run.py`` outer mean, generalised to
    per-scenario ``n``. The pooled ``success_rate`` and its Wilson CI come from the summed counts.
    ``context`` prefixes the honest note.
    """
    for n, c in trials:
        _validate_trial(n, c)

    n_scenarios = len(trials)
    total_c = sum(c for _, c in trials)
    total_n = sum(n for n, _ in trials)
    max_trials = max((n for n, _ in trials), default=0)

    pass_hat: dict[int, float] = {}
    for k in range(1, max_trials + 1):
        eligible = [(n, c) for n, c in trials if n >= k]
        if not eligible:
            continue
        pass_hat[k] = round(sum(pass_hat_k(n, c, k) for n, c in eligible) / len(eligible), 4)

    success_rate = round(total_c / total_n, 4) if total_n else 0.0

    collapse = (
        "所有场景 n_trials=1（确定性离线评测），pass^k 仅在 k=1 有定义，"
        "诚实退化为 pass@1（=成功率）；真正的 k>1 可靠性需重复采样，"
        "见 scripts/run_real_staged_world_seed.py 的 --samples 路径。"
        "不得对外声称为『k 次试验可靠性』，只是 pass@1 的另一种呈现。"
        if max_trials <= 1
        else "pass^k 为 τ-bench 悲观可靠性（k 次试验全部成功的概率），随 k 增大而衰减。"
    )
    note = " ".join(part for part in (context, collapse) if part)

    return ReliabilityReport(
        n_scenarios=n_scenarios,
        successes=total_c,
        trials=total_n,
        success_rate=success_rate,
        success_rate_ci=_wilson_interval(total_c, total_n),
        max_trials=max_trials,
        pass_hat_k=pass_hat,
        note=note,
    )
