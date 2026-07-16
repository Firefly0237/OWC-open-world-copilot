# 配对 A/B 检索评测：McNemar 精确检验 + 多跳准入门禁（替换 4 题、分母 0/0 的旧 A/B）。
"""Paired A/B evaluation for retrieval configs: McNemar's exact test + a multi-hop admission gate.

This replaces the retired prompt A/B (``benchmarks/prompt_ab_report.md`` — 4 questions, a ``0/0``
bridge-hit denominator) with a statistically honest, paired design over the frozen ``n >= 100``
retrieval evaluation set (:mod:`owcopilot.evaluation.retrieval_dataset`):

* **Paired success@k.** The *same* query is scored under configuration A and configuration B, so the
  two arms are compared per query, not as two independent marginal rates. This yields the 2x2
  contingency table (both hit / A-only / B-only / both miss) McNemar's test consumes.

* **McNemar's exact test** (McNemar 1947). The test conditions on the discordant pairs ``(b, c)``
  and, under H0, ``b ~ Binomial(b + c, 1/2)``. The two-sided p-value is the **exact** binomial tail
  ``p = 2 * min(P(X <= min(b, c)), 1/2)``, computed with pure :func:`math.comb` in exact rational
  arithmetic — no SciPy (it is not a project dependency), no normal approximation. The
  continuity-corrected chi-square ``(|b - c| - 1)^2 / (b + c)`` is reported as a *reference*
  statistic once the discordant count is large enough (``b + c >= 25``); the p-value is always the
  exact binomial. ``b + c == 0`` is defined as ``p = 1.0`` (no discordant evidence either way).

* **Multi-hop admission gate.** A multi-hop (indirect) query is admitted into the scored set only if
  *every hop* of its gold evidence chain resolves to a real node in the indexed corpus. Questions
  whose evidence chain is incomplete are rejected with a per-hop reason. This directly addresses the
  ``indirect recall@10 = 0.17`` observation: it separates "the question itself is unanswerable (its
  gold chain is not fully in the corpus)" from "retrieval failed to find answerable evidence", so a
  low recall number is not silently blamed on the retriever when the benchmark item was broken.

Scope / honesty. Like the recall dataset, the default runner drives the **production**
:class:`~owcopilot.retrieval.context_pack.ContextPackBuilder` under the deterministic
:class:`~owcopilot.llm.cache.HashingEmbedder`, so it is ``$0`` and reproducible. The absolute
per-arm hit-rates under the hashing embedder are **not** a claim about semantic quality (same caveat
the acceptance gate pins on its HashingEmbedder); what this module contributes is the *paired,
significance-tested methodology*. Inject a semantic embedder for a dated live number.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel, Field

from ..content.store import ContentStore
from ..llm.cache import Embedder, HashingEmbedder
from ..pipeline.project import ProjectContext
from ..retrieval.context_pack import ContextPackBuilder
from .acceptance import build_acceptance_world
from .retrieval_dataset import (
    DEFAULT_K_VALUES,
    RetrievalEvalDataset,
    RetrievalEvalQuery,
    RetrievalQueryCategory,
    build_retrieval_eval_dataset,
)

# A budget large enough that the token-budget trim is a no-op, so ``ContextPack.refs`` is the full
# ranked order (recall@k reads the ranking, not the post-budget pack). Mirrors the recall runner.
_UNTRIMMED_BUDGET = 1_000_000

#: The pre-registered headline operating point for the paired comparison. success@1 (precision@1) is
#: the strictest, least-saturated, most discriminating single point and the one the rerank/hybrid
#: change most directly moves; the full k-sweep is reported alongside so nothing is cherry-picked.
PRIMARY_K = 1

#: The discordant-pair count above which the continuity-corrected chi-square is reported as a
#: reference statistic (below it the normal approximation is unreliable and only the exact test is
#: shown). A conventional small-sample threshold for the McNemar chi-square approximation.
CHI_SQUARE_MIN_DISCORDANT = 25

# Human-readable arm labels for the first landed use case (a real one-line config switch on the
# production ContextPackBuilder: sparse-only recall vs the full hybrid recall + rerank pipeline).
ARM_A_LABEL = "bm25_only"
ARM_B_LABEL = "hybrid_rerank"


# --------------------------------------------------------------------------------------------------
# McNemar's exact test (McNemar 1947) — pure math.comb, no SciPy.
# --------------------------------------------------------------------------------------------------


class McNemarResult(BaseModel):
    """McNemar's paired test over the discordant pairs of a 2x2 table.

    ``b`` = pairs where arm A succeeded and arm B failed; ``c`` = arm A failed and arm B succeeded.
    ``p_value`` is the two-sided **exact** binomial tail. ``chi_square_cc`` is the continuity-
    corrected chi-square statistic, reported only when ``n_discordant >= CHI_SQUARE_MIN_DISCORDANT``
    (else ``None``), as a reference — the decision always uses ``p_value``."""

    b: int
    c: int
    n_discordant: int
    p_value: float
    chi_square_cc: float | None
    alpha: float
    significant: bool
    method: str = "mcnemar_exact_binomial"


def _binomial_cdf_half(n: int, x: int) -> Fraction:
    """Exact ``P(X <= x)`` for ``X ~ Binomial(n, 1/2)`` as a :class:`~fractions.Fraction`.

    ``n == 0`` is the empty binomial (``X == 0`` with probability 1), so the CDF is 1 for any
    ``x >= 0``. The tail is summed with integer :func:`math.comb`, never a float approximation."""
    if n == 0:
        return Fraction(1)
    numerator = sum(math.comb(n, i) for i in range(0, x + 1))
    return Fraction(numerator, 2**n)


def mcnemar_exact(b: int, c: int, *, alpha: float = 0.05) -> McNemarResult:
    """Two-sided McNemar exact binomial test on discordant counts ``b`` and ``c``.

    Under H0 (the two arms are equally likely to be the one that succeeds on a discordant pair),
    ``b ~ Binomial(b + c, 1/2)``. The exact two-sided p-value is ``2 * min(P(X <= min(b, c)), 1/2)``
    — the ``min(., 1/2)`` cap keeps ``p <= 1`` when ``b`` and ``c`` are close. ``b + c == 0`` is
    defined as ``p = 1.0`` (no discordant evidence). Textbook checks (see tests): ``(b, c)=(5, 15)``
    -> ``p ~= 0.0414``; ``(0, 8)`` -> ``p = 0.0078125``; ``b == c`` -> ``p = 1.0``.
    """
    if b < 0 or c < 0:
        raise ValueError(f"discordant counts must be non-negative, got b={b}, c={c}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")

    n = b + c
    if n == 0:
        return McNemarResult(
            b=b,
            c=c,
            n_discordant=0,
            p_value=1.0,
            chi_square_cc=None,
            alpha=alpha,
            significant=False,
        )

    tail = _binomial_cdf_half(n, min(b, c))
    p_value = float(2 * min(tail, Fraction(1, 2)))
    chi_square_cc = (abs(b - c) - 1) ** 2 / n if n >= CHI_SQUARE_MIN_DISCORDANT else None
    return McNemarResult(
        b=b,
        c=c,
        n_discordant=n,
        p_value=round(p_value, 6),
        chi_square_cc=round(chi_square_cc, 4) if chi_square_cc is not None else None,
        alpha=alpha,
        significant=p_value < alpha,
    )


# --------------------------------------------------------------------------------------------------
# Paired success@k contingency + A/B comparison (pure metric layer).
# --------------------------------------------------------------------------------------------------


class ContingencyTable(BaseModel):
    """The 2x2 paired-outcome table for one slice at one ``k``.

    ``a_only_hit`` and ``b_only_hit`` are the discordant cells McNemar conditions on (they map to
    the test's ``b`` and ``c`` respectively)."""

    n: int
    both_hit: int  # a: A hit AND B hit
    a_only_hit: int  # b: A hit, B miss  (McNemar b)
    b_only_hit: int  # c: A miss, B hit  (McNemar c)
    both_miss: int  # d: A miss AND B miss
    arm_a_hits: int
    arm_b_hits: int


class ABComparison(BaseModel):
    """A paired A/B comparison for one slice at one ``k``: the contingency table, per-arm success
    rates, and McNemar's test over the discordant pairs."""

    label: str
    k: int
    n: int
    contingency: ContingencyTable
    arm_a_success_rate: float
    arm_b_success_rate: float
    mcnemar: McNemarResult


def _is_hit(query: RetrievalEvalQuery, ranked_refs: Sequence[str], k: int) -> bool:
    """success@k: does any gold ref appear in the top-``k`` of the ranked list (Bernoulli trial)."""
    return bool(set(query.expected_refs) & set(list(ranked_refs)[:k]))


def contingency_at_k(
    queries: Sequence[RetrievalEvalQuery],
    ranked_a: dict[str, Sequence[str]],
    ranked_b: dict[str, Sequence[str]],
    *,
    k: int,
) -> ContingencyTable:
    """Build the 2x2 paired success@k table from the two arms' ranked lists (keyed by query id)."""
    both = a_only = b_only = neither = 0
    for query in queries:
        hit_a = _is_hit(query, ranked_a[query.query_id], k)
        hit_b = _is_hit(query, ranked_b[query.query_id], k)
        if hit_a and hit_b:
            both += 1
        elif hit_a:
            a_only += 1
        elif hit_b:
            b_only += 1
        else:
            neither += 1
    return ContingencyTable(
        n=len(queries),
        both_hit=both,
        a_only_hit=a_only,
        b_only_hit=b_only,
        both_miss=neither,
        arm_a_hits=both + a_only,
        arm_b_hits=both + b_only,
    )


def compare_at_k(
    label: str,
    queries: Sequence[RetrievalEvalQuery],
    ranked_a: dict[str, Sequence[str]],
    ranked_b: dict[str, Sequence[str]],
    *,
    k: int,
    alpha: float = 0.05,
) -> ABComparison:
    """Paired A/B comparison over ``queries`` at ``k``: contingency + per-arm rates + McNemar."""
    table = contingency_at_k(queries, ranked_a, ranked_b, k=k)
    mcnemar = mcnemar_exact(table.a_only_hit, table.b_only_hit, alpha=alpha)
    n = table.n
    return ABComparison(
        label=label,
        k=k,
        n=n,
        contingency=table,
        arm_a_success_rate=round(table.arm_a_hits / n, 4) if n else 0.0,
        arm_b_success_rate=round(table.arm_b_hits / n, 4) if n else 0.0,
        mcnemar=mcnemar,
    )


# --------------------------------------------------------------------------------------------------
# Multi-hop admission gate.
# --------------------------------------------------------------------------------------------------


class AdmissionRejection(BaseModel):
    """A multi-hop query rejected by the gate, with the hops whose gold evidence is not in the
    corpus. ``missing`` entries are ``{"hop_index": int, "missing_refs": [ref, ...]}``."""

    query_id: str
    missing: list[dict[str, object]]


class AdmissionReport(BaseModel):
    """The admission-gate outcome over the multi-hop (indirect) queries: how many were admitted,
    how many rejected, and — per rejection — which hop's gold evidence is absent from the corpus."""

    n_multihop: int
    n_admitted: int
    n_rejected: int
    admitted_ids: list[str] = Field(default_factory=list)
    rejected: list[AdmissionRejection] = Field(default_factory=list)


def admit_multihop_queries(
    dataset: RetrievalEvalDataset, corpus_node_refs: set[str]
) -> AdmissionReport:
    """Admit a multi-hop query only if every hop of its gold chain is in the indexed corpus.

    ``corpus_node_refs`` is the set of retrievable/citable node refs (``project.graph.node_refs``).
    For every query carrying an ``evidence_hops`` chain, each hop's refs are checked against that
    set; any hop with an absent ref rejects the query, reported with its hop index and missing refs.
    Single-hop queries carry no chain and are not subject to the gate."""
    admitted: list[str] = []
    rejected: list[AdmissionRejection] = []
    multihop = [q for q in dataset.queries if q.is_multihop]
    for query in multihop:
        missing: list[dict[str, object]] = []
        for hop_index, hop in enumerate(query.evidence_hops):
            absent = sorted(ref for ref in hop if ref not in corpus_node_refs)
            if absent:
                missing.append({"hop_index": hop_index, "missing_refs": absent})
        if missing:
            rejected.append(AdmissionRejection(query_id=query.query_id, missing=missing))
        else:
            admitted.append(query.query_id)
    return AdmissionReport(
        n_multihop=len(multihop),
        n_admitted=len(admitted),
        n_rejected=len(rejected),
        admitted_ids=admitted,
        rejected=rejected,
    )


# --------------------------------------------------------------------------------------------------
# Production-path runner: the first landed A/B use case.
# --------------------------------------------------------------------------------------------------


class RetrievalABReport(BaseModel):
    """The full paired retrieval A/B report over the frozen eval set.

    ``overall_by_k`` is the headline arm comparison over the admitted answerable queries at each
    ``k``; ``primary`` is the pre-registered ``PRIMARY_K`` comparison; ``by_category_primary``
    breaks the primary comparison down by query category. ``admission`` records which multi-hop
    questions were let in. Traceability mirrors the recall report (``dataset_version``/hash)."""

    dataset_version: str
    dataset_hash: str
    arm_a: str
    arm_b: str
    embedder: str
    corpus_size: int
    k_values: list[int]
    primary_k: int
    n_paired: int
    admission: AdmissionReport
    overall_by_k: dict[int, ABComparison]
    primary: ABComparison
    by_category_primary: dict[str, ABComparison] = Field(default_factory=dict)
    note: str = ""


def _arm_builders(project: ProjectContext) -> tuple[ContextPackBuilder, ContextPackBuilder]:
    """The two arms as one-line config switches on the production builder, reusing the project's
    already-built retrievers (same pattern as ``ProjectContext.qa_context_builder``).

    Arm A ``bm25_only``: sparse lexical recall + fusion, no vector leg, no graph expansion, rerank
    off. Arm B ``hybrid_rerank``: hybrid recall (bm25 + vector + graph) + the rerank precision stage
    (the lexical re-scorer here; the neural cross-encoder only wires on above the size gate)."""
    base = project.context_builder
    arm_a = ContextPackBuilder(bm25=base.bm25, rerank=False)
    arm_b = ContextPackBuilder(
        bm25=base.bm25,
        vector=base.vector,
        graph=base.graph,
        neural_reranker=base.neural_reranker,
        rerank=True,
    )
    return arm_a, arm_b


def run_retrieval_ab_eval(
    workspace: str | Path,
    *,
    dataset: RetrievalEvalDataset | None = None,
    embedder: Embedder | None = None,
    k_values: tuple[int, ...] = DEFAULT_K_VALUES,
    primary_k: int = PRIMARY_K,
    alpha: float = 0.05,
) -> RetrievalABReport:
    """Run the paired retrieval A/B (bm25-only vs hybrid+rerank) through the production path.

    Saves the acceptance world, opens a real :class:`ProjectContext`, runs the multi-hop admission
    gate against the indexed corpus, then scores both arms' ranked lists over the admitted
    answerable queries and compares them per ``k`` with McNemar's exact test. Defaults to the
    deterministic :class:`HashingEmbedder` so it is ``$0`` and reproducible (inject a semantic
    embedder for a dated live number). Not wired into the CI gate, like the recall runner."""
    if primary_k not in k_values:
        raise ValueError(f"primary_k={primary_k} must be one of k_values={k_values}")
    ds = dataset or build_retrieval_eval_dataset()
    root = Path(workspace)
    world_root = root / "ab_eval_world"
    ContentStore(world_root).save(build_acceptance_world())
    emb = embedder or HashingEmbedder()
    project = ProjectContext.open(world_root, sqlite_path=root / "ab_eval.sqlite", embedder=emb)
    try:
        corpus_node_refs = set(project.graph.node_refs())
        admission = admit_multihop_queries(ds, corpus_node_refs)
        rejected_ids = {rej.query_id for rej in admission.rejected}
        # Scored set = answerable queries that passed the gate (rejected multi-hop items are held
        # out, so a broken benchmark item can never depress the measured retrieval numbers).
        answerable = [q for q in ds.answerable if q.query_id not in rejected_ids]

        arm_a, arm_b = _arm_builders(project)
        limit = max(k_values)
        # Batch-encode each arm's queries in one embed_many. Arm A (bm25-only, no vector leg) has
        # nothing to encode and build_many degrades to a per-query loop; arm B (hybrid) batches its
        # dense-leg encodes. Bit-identical to per-query ``build`` under the deterministic embedder.
        texts = [q.text for q in answerable]
        packs_a = arm_a.build_many(texts, budget_tokens=_UNTRIMMED_BUDGET, limit=limit)
        packs_b = arm_b.build_many(texts, budget_tokens=_UNTRIMMED_BUDGET, limit=limit)
        ranked_a: dict[str, Sequence[str]] = {
            q.query_id: p.refs for q, p in zip(answerable, packs_a, strict=True)
        }
        ranked_b: dict[str, Sequence[str]] = {
            q.query_id: p.refs for q, p in zip(answerable, packs_b, strict=True)
        }

        overall_by_k = {
            k: compare_at_k("overall", answerable, ranked_a, ranked_b, k=k, alpha=alpha)
            for k in k_values
        }
        by_category_primary: dict[str, ABComparison] = {}
        for category in RetrievalQueryCategory:
            if category is RetrievalQueryCategory.UNANSWERABLE:
                continue
            members = [q for q in answerable if q.category is category]
            if members:
                by_category_primary[category.value] = compare_at_k(
                    category.value, members, ranked_a, ranked_b, k=primary_k, alpha=alpha
                )
        corpus_size = len(corpus_node_refs)
    finally:
        project.close()

    embedder_name = type(emb).__name__
    return RetrievalABReport(
        dataset_version=ds.version,
        dataset_hash=ds.content_digest(),
        arm_a=ARM_A_LABEL,
        arm_b=ARM_B_LABEL,
        embedder=embedder_name,
        corpus_size=corpus_size,
        k_values=list(k_values),
        primary_k=primary_k,
        n_paired=len(answerable),
        admission=admission,
        overall_by_k=overall_by_k,
        primary=overall_by_k[primary_k],
        by_category_primary=by_category_primary,
        note=(
            f"Paired success@k over the frozen eval set (embedder={embedder_name}); McNemar exact "
            "binomial p-values. Under HashingEmbedder the absolute hit-rates are not a semantic-"
            "quality claim (methodology demo); inject a semantic embedder for a live number."
        ),
    )
