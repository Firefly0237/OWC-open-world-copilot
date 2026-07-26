"""T15: staged FP2/FP3 retrieval diagnostic (Barnett et al. 2024, arXiv:2401.05856).

The acceptance gate's ``_retrieval_hit_rate`` reports ONE post-budget-pack number, conflating two
distinct Barnett failure points: FP2 "Missed the Top Ranked Documents" (the answer never entered the
returned pool) and FP3 "Not in Context — Consolidation strategy Limitations" (the answer was in the
pool but dropped during token-budget consolidation). This module splits them.

All tests are $0 and deterministic (HashingEmbedder / offline). The pure metric tests assemble
synthetic oracles and assert the arithmetic of the split; the production tests run the split through
the real :class:`ContextPackBuilder` and prove the fail-closed FP3 <= FP2 invariant holds on real
data. No business logic is duplicated — the split's own machinery is exercised, and the Wilson CI
reuses the production ``_wilson_interval``.
"""

from __future__ import annotations

import inspect

import pytest

from owcopilot.assist.calibration import _wilson_interval
from owcopilot.evaluation.retrieval_dataset import (
    DEFAULT_K_VALUES,
    DEFAULT_STAGED_BUDGET,
    RetrievalEvalDataset,
    RetrievalEvalQuery,
    RetrievalQueryCategory,
    StagedRetrievalDiagnostic,
    build_retrieval_eval_dataset,
    evaluate_staged_retrieval,
    run_staged_retrieval_diagnostic,
)
from owcopilot.retrieval.context_pack import ContextPackBuilder

_CATS = [
    RetrievalQueryCategory.VERBATIM,
    RetrievalQueryCategory.PARAPHRASE,
    RetrievalQueryCategory.INDIRECT,
]
_LOCALES = ["zh-CN", "en"]


def _mixed_dataset(n_per_group: int = 6, *, unanswerable: int = 4) -> RetrievalEvalDataset:
    """A synthetic single-relevant dataset spanning every answerable category and both locales, plus
    an unanswerable tail — so overall / by_category / by_locale slices are all exercised. Pure input
    assembly (no business logic)."""
    queries: list[RetrievalEvalQuery] = []
    i = 0
    for cat in _CATS:
        for loc in _LOCALES:
            for _ in range(n_per_group):
                queries.append(
                    RetrievalEvalQuery(
                        query_id=f"m-{i:03d}",
                        text=f"q{i}",
                        locale=loc,
                        category=cat,
                        expected_refs=[f"entity:e{i}"],
                    )
                )
                i += 1
    for _ in range(unanswerable):
        queries.append(
            RetrievalEvalQuery(
                query_id=f"m-{i:03d}",
                text=f"un{i}",
                locale="en",
                category=RetrievalQueryCategory.UNANSWERABLE,
                expected_refs=[],
            )
        )
        i += 1
    return RetrievalEvalDataset(version="test", seed=0, world_content_hash="x", queries=queries)


def _all_slices(report) -> list[StagedRetrievalDiagnostic]:
    return [report.overall, *report.by_category.values(), *report.by_locale.values()]


# --------------------------------------------------------------------------------------------------
# Pure metric layer — synthetic oracles (pack subset of pool by construction).
# --------------------------------------------------------------------------------------------------


def test_staged_loss_equals_fp2_minus_fp3() -> None:
    # budget_trim_loss must be exactly the (rounded) difference of the two rates, for EVERY slice.
    ds = _mixed_dataset()
    answerable = ds.answerable
    # Pool holds the ref for all; pack keeps it for every third query -> a non-trivial FP3 leg.
    keep_in_pack = {q.query_id for i, q in enumerate(answerable) if i % 3 == 0}

    def pool_refs(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs)

    def pack_refs(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs) if q.query_id in keep_in_pack else []

    report = evaluate_staged_retrieval(ds, pool_refs, pack_refs, budget_tokens=800, pool_limit=10)
    for slice_ in _all_slices(report):
        assert slice_.budget_trim_loss == round(
            slice_.fp2_pool_hit_rate - slice_.fp3_pack_hit_rate, 4
        )


def test_staged_fp3_never_exceeds_fp2_pure() -> None:
    # FP3 is strictly downstream of FP2 (consolidation only removes), so FP3 <= FP2 and loss >= 0
    # for overall + every by_category + by_locale slice.
    ds = _mixed_dataset()
    answerable = ds.answerable
    keep_in_pack = {q.query_id for i, q in enumerate(answerable) if i % 2 == 0}

    def pool_refs(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs)

    def pack_refs(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs) if q.query_id in keep_in_pack else []

    report = evaluate_staged_retrieval(ds, pool_refs, pack_refs, budget_tokens=800, pool_limit=10)
    for slice_ in _all_slices(report):
        assert slice_.fp3_pack_hits <= slice_.fp2_pool_hits
        assert slice_.fp3_pack_hit_rate <= slice_.fp2_pool_hit_rate
        assert slice_.budget_trim_loss >= 0.0


def test_staged_loss_is_explainable() -> None:
    # Construct an oracle where exactly m answerable queries have the ref in the pool but trimmed
    # out of the pack. The overall loss IS the count of retrieved-but-consolidated-out queries / n.
    ds = _mixed_dataset()  # 3 cats x 2 locales x 6 = 36 answerable
    answerable = ds.answerable
    n = len(answerable)
    assert n == 36
    m = 9
    trimmed_ids = {q.query_id for q in answerable[:m]}

    def pool_refs(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs)  # every ref is in the pool -> FP2 = 100%

    def pack_refs(q: RetrievalEvalQuery) -> list[str]:
        # trimmed queries lose the ref during consolidation (empty is a valid subset of the pool)
        return [] if q.query_id in trimmed_ids else list(q.expected_refs)

    report = evaluate_staged_retrieval(ds, pool_refs, pack_refs, budget_tokens=800, pool_limit=10)
    assert report.overall.fp2_pool_hits == n
    assert report.overall.fp3_pack_hits == n - m
    assert report.overall.fp2_pool_hits - report.overall.fp3_pack_hits == m
    assert round(report.overall.budget_trim_loss * report.overall.n) == m


def test_staged_invariant_rejects_pack_not_subset_of_pool() -> None:
    # Fail-closed: a pack carrying a ref absent from its pool means consolidation "added" a hit,
    # which is physically impossible for a trim stage -> ValueError, never a silent FP3 > FP2.
    ds = _mixed_dataset(n_per_group=1, unanswerable=0)

    def empty_pool(_q: RetrievalEvalQuery) -> list[str]:
        return []

    def pack_with_extra(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs)  # non-empty, but nothing is in the (empty) pool

    with pytest.raises(ValueError, match="budget trim can only drop hits"):
        evaluate_staged_retrieval(ds, empty_pool, pack_with_extra, budget_tokens=800, pool_limit=10)


def test_staged_unanswerable_excluded_from_denominator() -> None:
    # BEIR: only queries with a positive judgement count. The unanswerable tail never enters any
    # denominator, and the answerable categories' n sum back to the overall denominator.
    ds = _mixed_dataset()

    def pool_refs(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs)

    report = evaluate_staged_retrieval(ds, pool_refs, pool_refs, budget_tokens=800, pool_limit=10)
    assert report.scored_queries == len(ds.answerable)
    assert report.excluded_unanswerable == len(ds.unanswerable)
    assert report.overall.n == len(ds.answerable)
    assert "unanswerable" not in report.by_category
    assert sum(cat.n for cat in report.by_category.values()) == report.overall.n


def test_staged_wilson_ci_reuses_calibration() -> None:
    # 100 single-relevant queries; the pool hits all (FP2=100), the pack keeps 90 (FP3=90). Both CIs
    # must equal the reused project function verbatim (not a re-derivation).
    queries = [
        RetrievalEvalQuery(
            query_id=f"s-{i:03d}",
            text=f"q{i}",
            locale="en" if i % 2 else "zh-CN",
            category=RetrievalQueryCategory.PARAPHRASE,
            expected_refs=[f"entity:e{i}"],
        )
        for i in range(100)
    ]
    ds = RetrievalEvalDataset(version="test", seed=0, world_content_hash="x", queries=queries)
    keep_in_pack = {q.query_id for q in queries[:90]}

    def pool_refs(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs)

    def pack_refs(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs) if q.query_id in keep_in_pack else []

    report = evaluate_staged_retrieval(ds, pool_refs, pack_refs, budget_tokens=800, pool_limit=10)
    assert report.overall.fp2_pool_hits == 100
    assert report.overall.fp3_pack_hits == 90
    assert report.overall.fp2_wilson_ci == _wilson_interval(100, 100)
    assert report.overall.fp3_wilson_ci == _wilson_interval(90, 100)
    assert report.overall.fp3_wilson_ci == [0.8256, 0.9448]


# --------------------------------------------------------------------------------------------------
# Production retrieval path (real ContextPackBuilder), $0 / HashingEmbedder.
# --------------------------------------------------------------------------------------------------


def test_default_staged_budget_matches_production() -> None:
    # The FP3 leg must be measured at exactly the production consolidation budget. If the production
    # ContextPackBuilder.build default ever changes, this fails loudly so DEFAULT_STAGED_BUDGET is
    # kept in lockstep rather than silently diverging.
    default = inspect.signature(ContextPackBuilder.build).parameters["budget_tokens"].default
    assert DEFAULT_STAGED_BUDGET == default


def test_production_staged_run_is_structurally_complete(tmp_path) -> None:
    report = run_staged_retrieval_diagnostic(tmp_path)
    assert report.budget_tokens == DEFAULT_STAGED_BUDGET
    assert report.pool_limit == max(DEFAULT_K_VALUES)
    assert set(report.by_category) == {"verbatim", "paraphrase", "indirect"}
    assert set(report.by_locale) == {"zh-CN", "en"}
    assert report.scored_queries == len(build_retrieval_eval_dataset().answerable)
    for slice_ in _all_slices(report):
        assert 0 <= slice_.fp3_pack_hits <= slice_.fp2_pool_hits <= slice_.n


def test_production_staged_fp3_never_exceeds_fp2(tmp_path) -> None:
    # The FP3 <= FP2 invariant proven on the REAL fusion->rerank->trim pipeline: the run does not
    # raise (pack is a genuine subset of pool), and every slice reports loss >= 0 with valid CIs.
    report = run_staged_retrieval_diagnostic(tmp_path)
    for slice_ in _all_slices(report):
        assert slice_.fp3_pack_hits <= slice_.fp2_pool_hits
        assert slice_.fp3_pack_hit_rate <= slice_.fp2_pool_hit_rate + 1e-9
        assert slice_.budget_trim_loss >= -1e-9
        for ci in (slice_.fp2_wilson_ci, slice_.fp3_wilson_ci):
            if ci is not None:
                low, high = ci
                assert 0.0 <= low <= high <= 1.0
