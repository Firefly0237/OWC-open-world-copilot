"""Run the frozen 120-query, $0 retrieval mechanism diagnostics.

This entry point composes the existing production-path runners; it does not implement a second
retriever or a second statistics layer.  The deterministic HashingEmbedder keeps the run offline
and reproducible, so the report is evidence for ContextPack plumbing, FP2/FP3 failure attribution,
and paired McNemar methodology.  It is deliberately not presented as semantic-model quality.

    python scripts/run_frozen_retrieval_diagnostics.py
    python scripts/run_frozen_retrieval_diagnostics.py --output .tmp/retrieval-diagnostics.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from owcopilot.evaluation.ab_compare import run_retrieval_ab_eval  # noqa: E402
from owcopilot.evaluation.retrieval_dataset import (  # noqa: E402
    CANONICAL_TEXT_ARTIFACT_FORMAT,
    DEFAULT_K_VALUES,
    DEFAULT_STAGED_BUDGET,
    FROZEN_DATASET_PATH,
    RetrievalEvalDataset,
    canonical_text_artifact_sha256,
    load_frozen_dataset,
    run_retrieval_recall_eval,
    run_staged_retrieval_diagnostic,
)
from owcopilot.llm.cache import HashingEmbedder  # noqa: E402
from owcopilot.util import use_utf8_stdout  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT.joinpath(
    "benchmarks", "evaluation", "reports", "retrieval-diagnostics-v1.json"
)
EXPECTED_QUERY_COUNT = 120


def _query_counts(dataset: RetrievalEvalDataset) -> dict[str, object]:
    return {
        "total": len(dataset.queries),
        "answerable": len(dataset.answerable),
        "unanswerable": len(dataset.unanswerable),
        "by_category": dict(sorted(Counter(q.category.value for q in dataset.queries).items())),
        "by_locale": dict(sorted(Counter(q.locale for q in dataset.queries).items())),
    }


def build_report(
    *,
    budget_tokens: int = DEFAULT_STAGED_BUDGET,
    pool_limit: int = max(DEFAULT_K_VALUES),
) -> dict[str, object]:
    """Call the three existing production runners and return a deterministic report payload."""
    dataset = load_frozen_dataset()
    if len(dataset.queries) != EXPECTED_QUERY_COUNT:
        raise RuntimeError(
            f"frozen retrieval contract requires {EXPECTED_QUERY_COUNT} queries; "
            f"found {len(dataset.queries)}"
        )
    if budget_tokens <= 0:
        raise ValueError("budget_tokens must be positive")
    if pool_limit <= 0:
        raise ValueError("pool_limit must be positive")

    embedder = HashingEmbedder()
    # Fail closed against a caller's shell opting into a neural reranker.  The frozen diagnostic is
    # an offline mechanism report; the separate semantic benchmark owns real-model quality claims.
    with (
        patch.dict(os.environ, {"OWCOPILOT_RERANKER": "lexical"}),
        tempfile.TemporaryDirectory(prefix="owcopilot-frozen-retrieval-") as workspace,
    ):
        recall = run_retrieval_recall_eval(
            workspace,
            dataset=dataset,
            embedder=embedder,
            k_values=DEFAULT_K_VALUES,
        )
        staged = run_staged_retrieval_diagnostic(
            workspace,
            dataset=dataset,
            embedder=embedder,
            budget_tokens=budget_tokens,
            pool_limit=pool_limit,
        )
        paired_ab = run_retrieval_ab_eval(
            workspace,
            dataset=dataset,
            embedder=embedder,
            k_values=DEFAULT_K_VALUES,
        )

    dataset_digest = dataset.content_digest()
    dataset_hashes_match = {
        recall.dataset_hash,
        staged.dataset_hash,
        paired_ab.dataset_hash,
    } == {dataset_digest}
    fp3_not_above_fp2 = (
        staged.overall.fp3_pack_hits <= staged.overall.fp2_pool_hits
        and staged.overall.budget_trim_loss >= 0
    )
    paired_denominator_matches = all(
        comparison.n == paired_ab.n_paired for comparison in paired_ab.overall_by_k.values()
    )
    checks = {
        "frozen_query_count_is_120": len(dataset.queries) == EXPECTED_QUERY_COUNT,
        "runner_dataset_hashes_match": dataset_hashes_match,
        "recall_denominator_matches_answerable": recall.scored_queries == len(dataset.answerable),
        "fp3_not_above_fp2": fp3_not_above_fp2,
        "mcnemar_uses_paired_denominator": paired_denominator_matches,
        "hashing_embedder_is_explicit": paired_ab.embedder == type(embedder).__name__,
    }
    if not all(checks.values()):
        failed = sorted(name for name, passed in checks.items() if not passed)
        raise RuntimeError(f"frozen retrieval diagnostic contract failed: {failed}")

    return {
        "schema_version": "retrieval-diagnostics-v1",
        "status": "mechanism_verified",
        "claim_boundary": {
            "semantic_quality_claim": False,
            "statement": (
                "HashingEmbedder validates deterministic production ContextPack mechanics, "
                "FP2/FP3 failure attribution, and paired McNemar A/B plumbing only. Its absolute "
                "recall and hit rates are not evidence of semantic embedding or neural-reranker "
                "quality; use retrieval-report-v1.json for the revision-pinned real-model result."
            ),
            "real_model_report": "benchmarks/evaluation/reports/retrieval-report-v1.json",
        },
        "execution": {
            "cost_usd": 0.0,
            "model_calls": 0,
            "network_calls": 0,
            "embedder": type(embedder).__name__,
            "embedder_model_id": embedder.model_id,
            "reranker": "lexical",
            "production_path": "ProjectContext -> ContextPackBuilder",
            "runners": [
                "run_retrieval_recall_eval",
                "run_staged_retrieval_diagnostic",
                "run_retrieval_ab_eval",
            ],
        },
        "dataset": {
            "path": FROZEN_DATASET_PATH.relative_to(REPO_ROOT).as_posix(),
            "version": dataset.version,
            "seed": dataset.seed,
            "content_digest": dataset_digest,
            "artifact_sha256": canonical_text_artifact_sha256(FROZEN_DATASET_PATH.read_bytes()),
            "artifact_sha256_canonicalization": CANONICAL_TEXT_ARTIFACT_FORMAT,
            "world_content_hash": dataset.world_content_hash,
            "queries": _query_counts(dataset),
        },
        "checks": checks,
        "recall": recall.model_dump(mode="json"),
        "fp2_fp3": staged.model_dump(mode="json"),
        "paired_mcnemar_ab": paired_ab.model_dump(mode="json"),
    }


def render_report(report: dict[str, object]) -> str:
    """Return the canonical, byte-stable JSON representation used by the committed report."""
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> None:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--budget-tokens", type=int, default=DEFAULT_STAGED_BUDGET)
    parser.add_argument("--pool-limit", type=int, default=max(DEFAULT_K_VALUES))
    args = parser.parse_args()

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_report(build_report(budget_tokens=args.budget_tokens, pool_limit=args.pool_limit)),
        encoding="utf-8",
    )
    print(f"[written] {output}")


if __name__ == "__main__":
    main()
