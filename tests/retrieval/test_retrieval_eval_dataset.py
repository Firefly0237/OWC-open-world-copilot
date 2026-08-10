"""T14: the frozen, versioned retrieval evaluation set (n>=100) + recall@k + Wilson CI.

All tests are $0 and deterministic (HashingEmbedder / offline QA). They assert **structural**
integrity and the **mathematical** recall@k monotonicity only — never a quality threshold, because
under HashingEmbedder the absolute recall is not a claim about semantic quality (quality numbers
belong in a dated live report). Business paths run through production code: the frozen set derives
from ``build_acceptance_world()``; label refs resolve through ``ProjectContext``; recall runs
through the production ``ContextPackBuilder``; refusal runs through the production QA service.
"""

from __future__ import annotations

from collections import Counter

from owcopilot.assist.calibration import _wilson_interval
from owcopilot.content.hash import content_hash
from owcopilot.evaluation.acceptance import build_acceptance_world
from owcopilot.evaluation.retrieval_dataset import (
    DEFAULT_K_VALUES,
    FROZEN_DATASET_PATH,
    CategoryRecall,
    RetrievalEvalDataset,
    RetrievalEvalQuery,
    RetrievalQueryCategory,
    RetrievalQuerySource,
    RetrievalRecallReport,
    build_retrieval_eval_dataset,
    evaluate_retrieval_recall,
    load_frozen_dataset,
    run_retrieval_recall_eval,
)
from owcopilot.llm.cache import HashingEmbedder, NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.pipeline.project import ProjectContext
from owcopilot.qa.offline import OfflineQAProvider
from owcopilot.qa.service import LoreQAService


def _dataset() -> RetrievalEvalDataset:
    return build_retrieval_eval_dataset()


# --------------------------------------------------------------------------------------------------
# A.1 — size floors + locale balance (BEIR/MTEB decomposition)
# --------------------------------------------------------------------------------------------------


def test_answerable_and_unanswerable_floors() -> None:
    ds = _dataset()
    assert len(ds.answerable) >= 100
    assert len(ds.unanswerable) >= 15


def test_per_category_floors() -> None:
    ds = _dataset()
    counts = Counter(q.category for q in ds.queries)
    assert counts[RetrievalQueryCategory.VERBATIM] >= 30
    assert counts[RetrievalQueryCategory.PARAPHRASE] >= 40
    assert counts[RetrievalQueryCategory.INDIRECT] >= 15
    assert counts[RetrievalQueryCategory.UNANSWERABLE] >= 15


def test_locale_balance_at_least_40pct_each() -> None:
    ds = _dataset()
    total = len(ds.queries)
    locales = Counter(q.locale for q in ds.queries)
    assert set(locales) == {"zh-CN", "en"}
    assert locales["zh-CN"] / total >= 0.40
    assert locales["en"] / total >= 0.40


def test_query_ids_are_unique_and_texts_nonempty() -> None:
    ds = _dataset()
    ids = [q.query_id for q in ds.queries]
    assert len(ids) == len(set(ids))
    assert all(q.text.strip() for q in ds.queries)


def test_only_unanswerable_lack_expected_refs() -> None:
    ds = _dataset()
    for q in ds.queries:
        if q.category is RetrievalQueryCategory.UNANSWERABLE:
            assert q.expected_refs == [] and not q.is_answerable
        else:
            assert q.expected_refs and q.is_answerable


def test_default_source_is_template() -> None:
    # The $0 build is pure template expansion; the ARES-style llm_paraphrase source is opt-in only.
    ds = _dataset()
    assert {q.source for q in ds.queries} == {RetrievalQuerySource.TEMPLATE}


# --------------------------------------------------------------------------------------------------
# A.2 — label completeness through the production ProjectContext path
# --------------------------------------------------------------------------------------------------


def test_every_expected_ref_exists_in_the_world(tmp_path) -> None:
    from owcopilot.content.store import ContentStore

    ds = _dataset()
    world_root = tmp_path / "world"
    ContentStore(world_root).save(build_acceptance_world())
    project = ProjectContext.open(world_root, sqlite_path=tmp_path / "labels.sqlite")
    try:
        node_refs = set(project.graph.node_refs())
    finally:
        project.close()
    unknown = [
        (q.query_id, r) for q in ds.answerable for r in q.expected_refs if r not in node_refs
    ]
    assert unknown == [], f"expected_refs missing from the world graph: {unknown[:10]}"


# --------------------------------------------------------------------------------------------------
# A.3 — paraphrase purity (no name / alias leak) + indirect graph-hop purity
# --------------------------------------------------------------------------------------------------


def test_paraphrase_queries_do_not_contain_target_name_or_alias() -> None:
    bundle = build_acceptance_world()
    ds = _dataset()
    leaks: list[tuple[str, str]] = []
    for q in ds.queries:
        if q.category is not RetrievalQueryCategory.PARAPHRASE:
            continue
        for ref in q.expected_refs:
            entity = bundle.entities[ref.split(":", 1)[1]]
            for name in (entity.name, *entity.aliases):
                if len(name) >= 2 and name in q.text:
                    leaks.append((q.query_id, name))
    assert leaks == [], f"paraphrase queries leaked the target name/alias (verbatim leak): {leaks}"


def test_indirect_queries_do_not_contain_target_faction_name() -> None:
    # Indirect = graph hop: the answer faction is described only via the quest/location chain, never
    # by name — otherwise BM25 answers it without traversing any edge.
    bundle = build_acceptance_world()
    ds = _dataset()
    leaks: list[tuple[str, str]] = []
    for q in ds.queries:
        if q.category is not RetrievalQueryCategory.INDIRECT:
            continue
        for ref in q.expected_refs:
            entity = bundle.entities[ref.split(":", 1)[1]]
            for name in (entity.name, *entity.aliases):
                if len(name) >= 2 and name in q.text:
                    leaks.append((q.query_id, name))
    assert leaks == []


# --------------------------------------------------------------------------------------------------
# A.4 — determinism + frozen-set drift (MTEB freeze anchor)
# --------------------------------------------------------------------------------------------------


def test_build_is_deterministic() -> None:
    assert build_retrieval_eval_dataset() == build_retrieval_eval_dataset()
    assert (
        build_retrieval_eval_dataset().content_digest()
        == build_retrieval_eval_dataset().content_digest()
    )


def test_world_content_hash_pins_the_source_world() -> None:
    ds = _dataset()
    assert ds.world_content_hash == content_hash(build_acceptance_world())


def test_frozen_file_matches_live_build() -> None:
    # If the world or the builder drifts, the committed frozen set no longer matches and this fails
    # loudly (the whole point of a versioned, pinned eval set).
    assert FROZEN_DATASET_PATH.exists(), f"frozen set missing: {FROZEN_DATASET_PATH}"
    assert load_frozen_dataset() == build_retrieval_eval_dataset()


# --------------------------------------------------------------------------------------------------
# recall@k evaluation — pure metric layer (synthetic oracle)
# --------------------------------------------------------------------------------------------------


def _single_ref_dataset(n: int) -> RetrievalEvalDataset:
    queries = [
        RetrievalEvalQuery(
            query_id=f"s-{i:03d}",
            text=f"q{i}",
            locale="en" if i % 2 else "zh-CN",
            category=RetrievalQueryCategory.PARAPHRASE,
            expected_refs=[f"entity:e{i}"],
        )
        for i in range(n)
    ]
    return RetrievalEvalDataset(version="test", seed=0, world_content_hash="x", queries=queries)


def test_wilson_ci_reuses_calibration_golden() -> None:
    # 90 of 100 single-relevant queries hit at every k -> success@k = 90, n = 100.
    # The reused _wilson_interval must give the project's corrected golden [0.8256, 0.9448].
    ds = _single_ref_dataset(100)
    hit_ids = {q.query_id for q in ds.queries[:90]}

    def oracle(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs) if q.query_id in hit_ids else ["entity:other"]

    report = evaluate_retrieval_recall(ds, oracle)
    for k in DEFAULT_K_VALUES:
        assert report.overall.success_at_k[k] == 90
        assert report.overall.recall_at_k[k] == 0.9
        # identical to the standalone reused function — not a re-derivation
        assert report.overall.wilson_ci_at_k[k] == _wilson_interval(90, 100)
    assert report.overall.wilson_ci_at_k[1] == [0.8256, 0.9448]


def test_recall_monotonic_non_decreasing_in_k_pure() -> None:
    # Rank the correct ref at position (index % 10): recall@k must be non-decreasing as k grows
    # because top-k is a prefix of top-(k+1). Holds for the pure metric regardless of quality.
    ds = _single_ref_dataset(30)

    def oracle(q: RetrievalEvalQuery) -> list[str]:
        pos = int(q.query_id.split("-")[1]) % 10
        ranked = ["entity:filler"] * pos + list(q.expected_refs) + ["entity:tail"] * 10
        return ranked

    report = evaluate_retrieval_recall(ds, oracle)
    _assert_report_monotonic(report)


def test_unanswerable_excluded_from_recall_denominator() -> None:
    # BEIR: only queries with a positive judgement count toward recall. The whole dataset's
    # unanswerable class must never enter any denominator.
    ds = _dataset()

    def oracle(q: RetrievalEvalQuery) -> list[str]:
        return list(q.expected_refs)  # perfect oracle; never called for unanswerable

    report = evaluate_retrieval_recall(ds, oracle)
    assert report.scored_queries == len(ds.answerable)
    assert report.excluded_unanswerable == len(ds.unanswerable)
    assert report.overall.n == len(ds.answerable)
    assert "unanswerable" not in report.by_category
    # sum of the answerable categories' n equals the overall denominator
    assert sum(cat.n for cat in report.by_category.values()) == report.overall.n


# --------------------------------------------------------------------------------------------------
# recall@k evaluation — production retrieval path (ContextPackBuilder), $0 / HashingEmbedder
# --------------------------------------------------------------------------------------------------


def _assert_slice_monotonic(slice_: CategoryRecall) -> None:
    ks = sorted(slice_.recall_at_k)
    recalls = [slice_.recall_at_k[k] for k in ks]
    successes = [slice_.success_at_k[k] for k in ks]
    assert all(recalls[i] <= recalls[i + 1] + 1e-9 for i in range(len(recalls) - 1)), (
        f"recall@k not monotonic for slice {slice_.label!r}: {recalls}"
    )
    assert all(successes[i] <= successes[i + 1] for i in range(len(successes) - 1))
    for ci in slice_.wilson_ci_at_k.values():
        if ci is not None:
            low, high = ci
            assert 0.0 <= low <= high <= 1.0


def _assert_report_monotonic(report: RetrievalRecallReport) -> None:
    for slice_ in (report.overall, *report.by_category.values(), *report.by_locale.values()):
        _assert_slice_monotonic(slice_)


def test_production_recall_run_is_structurally_complete(tmp_path) -> None:
    report = run_retrieval_recall_eval(tmp_path)
    assert report.k_values == list(DEFAULT_K_VALUES)
    assert set(report.by_category) == {"verbatim", "paraphrase", "indirect"}
    assert set(report.by_locale) == {"zh-CN", "en"}
    for slice_ in (report.overall, *report.by_category.values(), *report.by_locale.values()):
        assert set(slice_.recall_at_k) == set(DEFAULT_K_VALUES)
        assert set(slice_.success_at_k) == set(DEFAULT_K_VALUES)
        assert set(slice_.wilson_ci_at_k) == set(DEFAULT_K_VALUES)


def test_production_recall_is_monotonic_over_all_slices(tmp_path) -> None:
    # The monotonicity acceptance test through the real ContextPackBuilder (recall reads the ranked
    # list; top-k is a prefix of top-(k+1)).
    report = run_retrieval_recall_eval(tmp_path)
    _assert_report_monotonic(report)


# --------------------------------------------------------------------------------------------------
# refusal teeth for the negative class — reused offline QA path
# --------------------------------------------------------------------------------------------------


def test_unanswerable_are_refused(tmp_path) -> None:
    # Genuinely out-of-world queries must be refused by the SAME offline QA path the acceptance gate
    # uses (LoreQAService + OfflineQAProvider), at $0. This gives the negative class real teeth.
    from owcopilot.content.store import ContentStore

    ds = _dataset()
    world_root = tmp_path / "world"
    ContentStore(world_root).save(build_acceptance_world())
    project = ProjectContext.open(
        world_root, sqlite_path=tmp_path / "qa.sqlite", embedder=HashingEmbedder()
    )
    try:
        qa = LoreQAService(
            gateway=LLMGateway(
                providers={"cheap": OfflineQAProvider()},
                router=StaticRouter(mapping={"qa_answer": "cheap", "qa_expand": "cheap"}),
                cache=NoOpCache(),
                telemetry=TelemetryCollector(),
            ),
            context_builder=project.qa_context_builder(),
            bundle=project.bundle,
        )
        not_refused = [
            q.text for q in ds.unanswerable if not qa.ask(q.text, budget_tokens=700).refused
        ]
    finally:
        project.close()
    assert not_refused == [], f"out-of-world queries were not refused: {not_refused}"
