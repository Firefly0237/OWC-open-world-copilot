"""Paired A/B retrieval evaluation: McNemar's exact test + the multi-hop admission gate.

All tests are $0 and deterministic (HashingEmbedder / pure math). The McNemar exact test is pinned
to textbook values; the paired contingency and the admission gate run over the production frozen
eval set and the real world graph. Business paths run through production code: the eval set derives
from ``build_acceptance_world()``; the arms are the production ``ContextPackBuilder`` under a real
config switch; corpus membership is the real ``ProjectContext`` graph node set.
"""

from __future__ import annotations

import math
from fractions import Fraction

from owcopilot.evaluation.ab_compare import (
    admit_multihop_queries,
    compare_at_k,
    contingency_at_k,
    mcnemar_exact,
    run_retrieval_ab_eval,
)
from owcopilot.evaluation.acceptance import build_acceptance_world
from owcopilot.evaluation.retrieval_dataset import (
    RetrievalEvalDataset,
    RetrievalEvalQuery,
    RetrievalQueryCategory,
    build_retrieval_eval_dataset,
)
from owcopilot.graph.index import build_content_graph

# --------------------------------------------------------------------------------------------------
# McNemar's exact test — textbook golden values (McNemar 1947, exact binomial version)
# --------------------------------------------------------------------------------------------------


def test_mcnemar_exact_textbook_5_15() -> None:
    # b=5, c=15: n=20 discordant, exact two-sided p = 2 * P(X<=5), X~Binom(20, 1/2) ~= 0.0414.
    result = mcnemar_exact(5, 15)
    assert result.n_discordant == 20
    assert result.b == 5 and result.c == 15
    assert abs(result.p_value - 0.041389) < 1e-6
    assert result.significant is True  # < 0.05


def test_mcnemar_exact_extreme_0_8() -> None:
    # b=0, c=8: p = 2 * P(X<=0) = 2 * C(8,0)/2^8 = 2/256 = 0.0078125 exactly.
    result = mcnemar_exact(0, 8)
    assert result.p_value == 0.007812  # rounded to 6 dp
    assert result.significant is True


def test_mcnemar_exact_symmetric_is_one() -> None:
    # b == c: the exact two-sided p-value is exactly 1.0 (max symmetry, no evidence of asymmetry).
    for b in (1, 5, 10, 13):
        assert mcnemar_exact(b, b).p_value == 1.0
        assert mcnemar_exact(b, b).significant is False


def test_mcnemar_exact_no_discordant_is_one() -> None:
    # b + c == 0: defined as p = 1.0 (documented edge case; no discordant evidence either way).
    result = mcnemar_exact(0, 0)
    assert result.n_discordant == 0
    assert result.p_value == 1.0
    assert result.significant is False
    assert result.chi_square_cc is None


def test_mcnemar_exact_matches_brute_force_binomial() -> None:
    # The exact tail must equal the brute-force 2 * sum_{i=0}^{min(b,c)} C(n,i)/2^n (capped at 1).
    for b, c in [(3, 9), (7, 7), (0, 4), (12, 20), (2, 30)]:
        n = b + c
        tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1))
        expected = float(min(Fraction(2 * tail, 2**n), Fraction(1)))
        assert abs(mcnemar_exact(b, c).p_value - round(expected, 6)) < 1e-9


def test_mcnemar_chi_square_only_reported_when_discordant_large() -> None:
    # Continuity-corrected chi-square (|b-c|-1)^2/(b+c) is a reference stat, reported only at n>=25.
    small = mcnemar_exact(5, 15)  # n=20 < 25
    assert small.chi_square_cc is None
    large = mcnemar_exact(20, 5)  # n=25
    assert large.chi_square_cc == round((abs(20 - 5) - 1) ** 2 / 25, 4)  # 14^2/25 = 7.84


def test_mcnemar_rejects_negative_counts() -> None:
    import pytest

    with pytest.raises(ValueError):
        mcnemar_exact(-1, 3)


# --------------------------------------------------------------------------------------------------
# Paired success@k contingency + comparison (pure metric layer, synthetic oracle)
# --------------------------------------------------------------------------------------------------


def _q(query_id: str, ref: str) -> RetrievalEvalQuery:
    return RetrievalEvalQuery(
        query_id=query_id,
        text=query_id,
        locale="en",
        category=RetrievalQueryCategory.PARAPHRASE,
        expected_refs=[ref],
    )


def test_contingency_partitions_all_pairs() -> None:
    # Arm A hits q0,q1; arm B hits q1,q2; q3 neither. -> both=1, a_only=1, b_only=1, neither=1.
    queries = [_q(f"q{i}", f"entity:e{i}") for i in range(4)]
    ranked_a = {
        "q0": ["entity:e0"],
        "q1": ["entity:e1"],
        "q2": ["entity:x"],
        "q3": ["entity:x"],
    }
    ranked_b = {
        "q0": ["entity:x"],
        "q1": ["entity:e1"],
        "q2": ["entity:e2"],
        "q3": ["entity:x"],
    }
    table = contingency_at_k(queries, ranked_a, ranked_b, k=1)
    assert (table.both_hit, table.a_only_hit, table.b_only_hit, table.both_miss) == (1, 1, 1, 1)
    assert table.arm_a_hits == 2 and table.arm_b_hits == 2
    assert table.n == 4


def test_compare_maps_discordant_cells_to_mcnemar_bc() -> None:
    # McNemar's b/c must be exactly the a_only/b_only discordant cells.
    queries = [_q(f"q{i}", f"entity:e{i}") for i in range(4)]
    ranked_a = {"q0": ["entity:e0"], "q1": ["entity:e1"], "q2": ["z"], "q3": ["z"]}
    ranked_b = {"q0": ["z"], "q1": ["entity:e1"], "q2": ["entity:e2"], "q3": ["z"]}
    comp = compare_at_k("t", queries, ranked_a, ranked_b, k=1)
    assert comp.mcnemar.b == comp.contingency.a_only_hit
    assert comp.mcnemar.c == comp.contingency.b_only_hit


def test_success_at_k_reads_topk_prefix() -> None:
    # The gold ref sits at rank 3 (0-indexed 2): a miss at k<=2, a hit at k>=3.
    query = _q("q0", "entity:gold")
    ranked = {"q0": ["a", "b", "entity:gold", "c"]}
    assert contingency_at_k([query], ranked, ranked, k=2).both_hit == 0
    assert contingency_at_k([query], ranked, ranked, k=3).both_hit == 1


# --------------------------------------------------------------------------------------------------
# Multi-hop admission gate
# --------------------------------------------------------------------------------------------------


def _corpus_node_refs() -> set[str]:
    return set(build_content_graph(build_acceptance_world()).node_refs())


def test_all_real_multihop_queries_are_admitted() -> None:
    # Every indirect query's gold chain is grounded in the real world graph, so the gate admits all.
    ds = build_retrieval_eval_dataset()
    report = admit_multihop_queries(ds, _corpus_node_refs())
    assert report.n_multihop == 24
    assert report.n_rejected == 0
    assert report.n_admitted == report.n_multihop
    assert report.rejected == []


def test_gate_rejects_a_query_with_a_missing_hop() -> None:
    # Negative control (teeth): a fabricated multi-hop query whose 2nd hop points at a non-existent
    # node must be rejected, with the missing hop index + ref surfaced (not silently admitted).
    broken = RetrievalEvalQuery(
        query_id="broken-001",
        text="which faction does the ghost serve",
        locale="en",
        category=RetrievalQueryCategory.INDIRECT,
        expected_refs=["entity:fac_iron"],
        evidence_hops=[["quest:quest_r1_q1"], ["entity:npc_does_not_exist"], ["entity:fac_iron"]],
    )
    ds = RetrievalEvalDataset(version="t", seed=0, world_content_hash="x", queries=[broken])
    report = admit_multihop_queries(ds, _corpus_node_refs())
    assert report.n_multihop == 1
    assert report.n_admitted == 0
    assert report.n_rejected == 1
    rej = report.rejected[0]
    assert rej.query_id == "broken-001"
    assert rej.missing == [{"hop_index": 1, "missing_refs": ["entity:npc_does_not_exist"]}]


def test_every_multihop_hop_ref_resolves_and_terminates_in_the_answer() -> None:
    # Integrity: each hop ref is a real corpus node, and the last hop is exactly the answer ref.
    ds = build_retrieval_eval_dataset()
    corpus = _corpus_node_refs()
    for q in ds.queries:
        if not q.is_multihop:
            continue
        for hop in q.evidence_hops:
            for ref in hop:
                assert ref in corpus, f"{q.query_id}: hop ref {ref} not in corpus"
        assert q.evidence_hops[-1] == q.expected_refs


# --------------------------------------------------------------------------------------------------
# Production-path runner (ContextPackBuilder arms, HashingEmbedder, $0)
# --------------------------------------------------------------------------------------------------


def test_ab_run_is_structurally_complete_and_paired(tmp_path) -> None:
    report = run_retrieval_ab_eval(tmp_path)
    # n>=100 paired samples (the whole point — replaces the old 4-question A/B).
    assert report.n_paired >= 100
    assert report.embedder == "HashingEmbedder"
    assert report.admission.n_multihop == 24
    assert report.admission.n_admitted == 24  # all real chains resolve
    assert report.primary_k in report.k_values
    assert set(report.overall_by_k) == set(report.k_values)
    assert set(report.by_category_primary) == {"verbatim", "paraphrase", "indirect"}
    # every k comparison is internally consistent: cells sum to n, and b/c map to the discordant set
    for comp in report.overall_by_k.values():
        c = comp.contingency
        assert c.both_hit + c.a_only_hit + c.b_only_hit + c.both_miss == comp.n
        assert comp.mcnemar.b == c.a_only_hit and comp.mcnemar.c == c.b_only_hit
        assert comp.n == report.n_paired


def test_ab_run_is_deterministic(tmp_path) -> None:
    r1 = run_retrieval_ab_eval(tmp_path / "a")
    r2 = run_retrieval_ab_eval(tmp_path / "b")
    assert r1.primary.model_dump() == r2.primary.model_dump()
    assert r1.n_paired == r2.n_paired
    assert r1.dataset_hash == r2.dataset_hash
