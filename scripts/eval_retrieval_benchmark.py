"""Run a frozen local BM25/dense/RRF/rerank benchmark.

The script tunes only RRF ``k``/weights/candidate depth on the declared dev split, then reports all
arms on held-out queries.  It fails if the embedding backend degrades from ``st:*`` or if a
requested neural reranker falls back, preventing a hashing/lexical run from being mislabeled as a
real-model result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from owcopilot.inspiration.models import (  # noqa: E402
    USE_INSPIRATION,
    Provenance,
    ReferenceChunk,
    ReferenceSource,
)
from owcopilot.inspiration.retrieval import ReferenceContextBuilder  # noqa: E402
from owcopilot.model_artifacts import is_immutable_revision  # noqa: E402
from owcopilot.retrieval.embedding import SemanticEmbedder  # noqa: E402
from owcopilot.retrieval.fusion import reciprocal_rank_fusion  # noqa: E402
from owcopilot.retrieval.models import RetrievalHit  # noqa: E402
from owcopilot.retrieval.neural_rerank import NeuralReranker  # noqa: E402
from owcopilot.retrieval.rerank import rerank_hits  # noqa: E402
from owcopilot.storage import SQLiteStore  # noqa: E402


def _load_dataset(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _seed_reference_index(store: SQLiteStore, documents: list[dict[str, str]]) -> None:
    joined = "\n".join(f"{row['title']}\t{row['body']}" for row in documents)
    now = datetime.now(UTC).isoformat()
    source = ReferenceSource(
        id="retrieval_eval_corpus",
        title="Retrieval benchmark corpus",
        source_type="frozen_eval",
        allowed_uses=[USE_INSPIRATION],
        provenance=Provenance.OWN_IP,
        text_hash=hashlib.sha256(joined.encode("utf-8")).hexdigest(),
        created_at=now,
        char_count=len(joined),
        chunk_count=len(documents),
        metadata={"dataset_role": "frozen_eval"},
    )
    chunks = [
        ReferenceChunk(
            id=row["id"],
            source_id=source.id,
            chunk_index=index,
            title=row["title"],
            body=row["body"],
            metadata={"eval_doc_id": row["id"]},
        )
        for index, row in enumerate(documents)
    ]
    store.replace_reference_index([source], chunks)


def _bm25_hits(store: SQLiteStore, query: str, *, limit: int) -> list[RetrievalHit]:
    return [
        RetrievalHit(
            ref=str(row["ref"]),
            object_type="reference_chunk",
            title=str(row["title"]),
            body=str(row["body"]),
            score=float(row["score"]),
            source="reference_bm25",
        )
        for row in store.search_reference_chunks(query, limit=limit)
    ]


def _metrics(
    queries: list[dict[str, Any]], rankings: dict[str, list[str]]
) -> dict[str, float]:
    reciprocal_ranks: list[float] = []
    ndcg5: list[float] = []
    recall = {1: 0, 3: 0, 5: 0}
    for query in queries:
        ranked = rankings[query["id"]]
        relevant = set(query["relevant"])
        rank = next((index for index, ref in enumerate(ranked, start=1) if ref in relevant), None)
        reciprocal_ranks.append(0.0 if rank is None else 1.0 / rank)
        ndcg5.append(0.0 if rank is None or rank > 5 else 1.0 / math.log2(rank + 1))
        for cutoff in recall:
            recall[cutoff] += int(bool(relevant.intersection(ranked[:cutoff])))
    count = len(queries)
    return {
        "mrr": sum(reciprocal_ranks) / count,
        "ndcg_at_5": sum(ndcg5) / count,
        **{f"recall_at_{cutoff}": hits / count for cutoff, hits in recall.items()},
    }


def _latency_summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "mean_ms": round(statistics.fmean(ordered), 3),
    }


def _rankings_for_config(
    cache: dict[str, dict[str, list[RetrievalHit]]],
    queries: list[dict[str, Any]],
    *,
    candidate_limit: int,
    rrf_k: int,
    weights: tuple[float, float],
) -> dict[str, list[str]]:
    rankings: dict[str, list[str]] = {}
    for query in queries:
        candidates = cache[query["id"]]
        fused = reciprocal_rank_fusion(
            [candidates["bm25"][:candidate_limit], candidates["dense"][:candidate_limit]],
            k=rrf_k,
            weights=weights,
            source="weighted_rrf",
        )
        rankings[query["id"]] = [hit.ref for hit in fused]
    return rankings


def run(args: argparse.Namespace) -> dict[str, Any]:
    dataset, dataset_sha256 = _load_dataset(args.dataset)
    queries = list(dataset["queries"])
    dev = [query for query in queries if query["split"] == "dev"]
    held_out = [query for query in queries if query["split"] == "held_out"]
    max_candidates = 20

    with tempfile.TemporaryDirectory(prefix="owcopilot-retrieval-eval-") as temp_dir:
        store = SQLiteStore(Path(temp_dir) / "eval.sqlite")
        try:
            _seed_reference_index(store, list(dataset["documents"]))
            embedder = SemanticEmbedder(
                args.embedding_model,
                revision=args.embedding_revision,
            )
            if not is_immutable_revision(embedder.model_revision):
                raise RuntimeError(
                    "formal retrieval evaluation requires an immutable embedding revision; "
                    "pass --embedding-revision with a 40-character model commit SHA"
                )
            builder = ReferenceContextBuilder(store, embedder=embedder, rerank=False)
            if builder.vector is None:
                raise RuntimeError("real vector retriever was not constructed")

            cache: dict[str, dict[str, list[RetrievalHit]]] = {}
            latencies: dict[str, list[float]] = {"bm25": [], "dense": []}
            for query in queries:
                started = time.perf_counter()
                bm25 = _bm25_hits(store, query["text"], limit=max_candidates)
                latencies["bm25"].append((time.perf_counter() - started) * 1000.0)
                started = time.perf_counter()
                dense = builder.vector.search(query["text"], limit=max_candidates)
                latencies["dense"].append((time.perf_counter() - started) * 1000.0)
                cache[query["id"]] = {"bm25": bm25, "dense": dense}

            if not builder.vector.model_id.startswith("st:"):
                raise RuntimeError(
                    f"semantic embedding degraded to {builder.vector.model_id!r}; refusing report"
                )
            embedding_dimension = len(embedder.embed(str(dataset["dataset_id"])))

            candidates: list[dict[str, Any]] = []
            for candidate_limit in (5, 10, 20):
                for rrf_k in (20, 60):
                    for weights in ((0.2, 0.8), (0.4, 0.6), (0.5, 0.5), (0.6, 0.4), (0.8, 0.2)):
                        rankings = _rankings_for_config(
                            cache,
                            dev,
                            candidate_limit=candidate_limit,
                            rrf_k=rrf_k,
                            weights=weights,
                        )
                        metric = _metrics(dev, rankings)
                        candidates.append(
                            {
                                "candidate_limit": candidate_limit,
                                "rrf_k": rrf_k,
                                "weights": list(weights),
                                "dev_metrics": metric,
                            }
                        )
            best = max(
                candidates,
                key=lambda row: (
                    row["dev_metrics"]["mrr"],
                    row["dev_metrics"]["recall_at_3"],
                    -row["candidate_limit"],
                    -abs(row["weights"][0] - 0.5),
                    -row["rrf_k"],
                ),
            )
            candidate_limit = int(best["candidate_limit"])
            rrf_k = int(best["rrf_k"])
            weights = (float(best["weights"][0]), float(best["weights"][1]))

            arms: dict[str, dict[str, list[str]]] = {
                "bm25": {},
                "dense_bge_m3": {},
                "rrf_equal": {},
                "rrf_weighted_dev_selected": {},
                "hybrid_lexical_semantic_rescore": {},
            }
            neural_rankings: dict[str, list[str]] = {}
            neural_latencies: list[float] = []
            neural_sources: set[str] = set()
            neural = (
                None
                if args.skip_neural
                else NeuralReranker(
                    args.reranker_model,
                    revision=args.reranker_revision,
                )
            )
            if neural is not None and not is_immutable_revision(neural.model_revision):
                raise RuntimeError(
                    "formal retrieval evaluation requires an immutable reranker revision; "
                    "pass --reranker-revision with a 40-character model commit SHA"
                )
            for query in held_out:
                cached = cache[query["id"]]
                bm25 = cached["bm25"][:candidate_limit]
                dense = cached["dense"][:candidate_limit]
                equal = reciprocal_rank_fusion([bm25, dense], k=rrf_k)
                weighted = reciprocal_rank_fusion(
                    [bm25, dense], k=rrf_k, weights=weights, source="weighted_rrf"
                )
                semantic_scores = builder.vector.similarities(
                    query["text"], [hit.ref for hit in weighted]
                )
                rescored = rerank_hits(query["text"], weighted, semantic_scores=semantic_scores)
                arms["bm25"][query["id"]] = [hit.ref for hit in bm25]
                arms["dense_bge_m3"][query["id"]] = [hit.ref for hit in dense]
                arms["rrf_equal"][query["id"]] = [hit.ref for hit in equal]
                arms["rrf_weighted_dev_selected"][query["id"]] = [
                    hit.ref for hit in weighted
                ]
                arms["hybrid_lexical_semantic_rescore"][query["id"]] = [
                    hit.ref for hit in rescored
                ]
                if neural is not None:
                    started = time.perf_counter()
                    neural_hits = neural.rerank(query["text"], weighted)
                    neural_latencies.append((time.perf_counter() - started) * 1000.0)
                    neural_sources.update(hit.source for hit in neural_hits)
                    neural_rankings[query["id"]] = [hit.ref for hit in neural_hits]

            if neural is not None:
                if neural_sources != {"reranked_neural"}:
                    raise RuntimeError(
                        f"neural reranker did not stay on real model path: {sorted(neural_sources)}"
                    )
                arms["hybrid_bge_reranker_v2_m3"] = neural_rankings

            return {
                "status": "passed",
                "dataset_id": dataset["dataset_id"],
                "dataset_sha256": dataset_sha256,
                "executed_at": datetime.now(UTC).isoformat(),
                "split": {"dev_queries": len(dev), "held_out_queries": len(held_out)},
                "model": {
                    "embedding_requested": args.embedding_model,
                    "embedding_revision": embedder.model_revision,
                    "embedding_effective": builder.vector.model_id,
                    "embedding_dimension": embedding_dimension,
                    "reranker_requested": None if neural is None else args.reranker_model,
                    "reranker_revision": None if neural is None else neural.model_revision,
                    "reranker_effective": None if neural is None else "real_cross_encoder",
                },
                "dev_selection": {
                    "search_space_size": len(candidates),
                    "primary_metric": "mrr",
                    "selected": best,
                    "held_out_not_used_for_selection": True,
                },
                "held_out_metrics": {
                    arm: _metrics(held_out, ranking) for arm, ranking in arms.items()
                },
                "latency": {
                    "bm25_query": _latency_summary(latencies["bm25"]),
                    "dense_query": _latency_summary(latencies["dense"]),
                    "neural_rerank_query": None
                    if not neural_latencies
                    else _latency_summary(neural_latencies),
                },
                "methodology": (
                    "Frozen synthetic regression dataset; configuration is dev-selected and "
                    "reported on held-out queries."
                ),
            }
        finally:
            store.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "evaluation" / "datasets" / "retrieval-qrels-v1.json",
    )
    parser.add_argument("--embedding-model", default="BAAI/bge-m3")
    parser.add_argument(
        "--embedding-revision",
        help=(
            "immutable model commit SHA; the packaged revision manifest supplies the default "
            "for BAAI/bge-m3"
        ),
    )
    parser.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3")
    parser.add_argument(
        "--reranker-revision",
        help=(
            "immutable model commit SHA; the packaged revision manifest supplies the default "
            "for BAAI/bge-reranker-v2-m3"
        ),
    )
    parser.add_argument("--skip-neural", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "evaluation" / "reports" / "retrieval-report-v1.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
