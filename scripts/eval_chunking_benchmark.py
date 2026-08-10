"""Evaluate structure-only vs real-embedding semantic chunk boundaries on frozen cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from owcopilot.intake import PdfTextLineLocator, SourceSegment  # noqa: E402
from owcopilot.model_artifacts import is_immutable_revision  # noqa: E402
from owcopilot.retrieval import ChunkingPolicy, build_retrieval_document  # noqa: E402
from owcopilot.retrieval.embedding import SemanticEmbedder  # noqa: E402


def _load(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _segments(document: dict[str, Any]) -> list[SourceSegment]:
    artifact_id = str(uuid5(NAMESPACE_URL, f"owcopilot:chunk-eval:{document['id']}"))
    rows: list[SourceSegment] = []
    for ordinal, item in enumerate(document["segments"]):
        rows.append(
            SourceSegment.from_text(
                segment_id=str(
                    uuid5(NAMESPACE_URL, f"owcopilot:chunk-eval:{document['id']}:{item['id']}")
                ),
                artifact_version_id=artifact_id,
                ordinal=ordinal,
                locator=PdfTextLineLocator(
                    page_index=int(item["page"]),
                    line_index=ordinal,
                    x0=72.0,
                    top=72.0 + ordinal * 24.0,
                    x1=520.0,
                    bottom=88.0 + ordinal * 24.0,
                    page_width=612.0,
                    page_height=792.0,
                    validation_backend_version="5.12.1",
                    extraction_backend_version="0.11.10",
                ),
                text=str(item["text"]),
            )
        )
    return rows


def _evaluate(
    documents: list[dict[str, Any]],
    *,
    policy: ChunkingPolicy,
    embedder: SemanticEmbedder | None,
    token_counter: Callable[[str], int],
    token_counter_id: str,
) -> dict[str, Any]:
    linked_correct = 0
    linked_total = 0
    separated_correct = 0
    separated_total = 0
    chunk_counts: list[int] = []
    token_counts: list[int] = []
    elapsed: list[float] = []
    per_document: list[dict[str, Any]] = []
    for source in documents:
        segments = _segments(source)
        local_to_uuid = {
            item["id"]: segment.segment_id
            for item, segment in zip(source["segments"], segments, strict=True)
        }
        started = time.perf_counter()
        result = build_retrieval_document(
            segments,
            policy=policy,
            embedder=embedder,
            token_counter=token_counter,
            token_counter_id=token_counter_id,
        )
        elapsed.append((time.perf_counter() - started) * 1000.0)
        memberships = [set(unit.segment_ids) for unit in result.units]

        link_results = [
            _together(pair, local_to_uuid=local_to_uuid, memberships=memberships)
            for pair in source["must_link"]
        ]
        separate_results = [
            not _together(pair, local_to_uuid=local_to_uuid, memberships=memberships)
            for pair in source["must_separate"]
        ]
        linked_correct += sum(link_results)
        linked_total += len(link_results)
        separated_correct += sum(separate_results)
        separated_total += len(separate_results)
        chunk_counts.append(len(result.units))
        token_counts.extend(unit.token_count for unit in result.units)
        per_document.append(
            {
                "id": source["id"],
                "chunks": len(result.units),
                "must_link_pass": sum(link_results),
                "must_link_total": len(link_results),
                "must_separate_pass": sum(separate_results),
                "must_separate_total": len(separate_results),
            }
        )
    link_rate = linked_correct / linked_total
    separate_rate = separated_correct / separated_total
    return {
        "boundary_balanced_accuracy": (link_rate + separate_rate) / 2.0,
        "must_link_preservation": link_rate,
        "must_separate_accuracy": separate_rate,
        "avg_chunks_per_document": statistics.fmean(chunk_counts),
        "avg_tokens_per_chunk": statistics.fmean(token_counts),
        "mean_latency_ms_per_document": statistics.fmean(elapsed),
        "per_document": per_document,
    }


def _together(
    pair: list[str],
    *,
    local_to_uuid: dict[str, str],
    memberships: list[set[str]],
) -> bool:
    expected = {local_to_uuid[pair[0]], local_to_uuid[pair[1]]}
    return any(expected.issubset(unit) for unit in memberships)


def run(args: argparse.Namespace) -> dict[str, Any]:
    dataset, dataset_hash = _load(args.dataset)
    dev = [row for row in dataset["documents"] if row["split"] == "dev"]
    held_out = [row for row in dataset["documents"] if row["split"] == "held_out"]
    embedder = SemanticEmbedder(
        args.embedding_model,
        revision=args.embedding_revision,
    )
    if not is_immutable_revision(embedder.model_revision):
        raise RuntimeError(
            "formal chunking evaluation requires an immutable embedding revision; "
            "pass --embedding-revision with a 40-character model commit SHA"
        )
    model: Any = embedder._ensure_model()  # evaluation needs the exact tokenizer of this model
    tokenizer = model.tokenizer

    def count_tokens(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False))

    token_counter_id = f"hf:{args.embedding_model}@{embedder.model_revision}"
    thresholds = (0.35, 0.5, 0.65, 0.8)
    dev_candidates: list[dict[str, Any]] = []
    for threshold in thresholds:
        policy = ChunkingPolicy(
            version=f"semantic-dev-threshold-{threshold}",
            strategy="semantic",
            min_tokens=12,
            target_tokens=32,
            max_tokens=64,
            overlap_tokens=0,
            semantic_breakpoint=threshold,
        )
        metrics = _evaluate(
            dev,
            policy=policy,
            embedder=embedder,
            token_counter=count_tokens,
            token_counter_id=token_counter_id,
        )
        dev_candidates.append({"threshold": threshold, "metrics": metrics})
    selected = max(
        dev_candidates,
        key=lambda row: (
            row["metrics"]["boundary_balanced_accuracy"],
            row["metrics"]["must_link_preservation"],
            -abs(row["threshold"] - 0.5),
        ),
    )
    semantic_policy = ChunkingPolicy(
        version="structure-semantic-parent-child-v1",
        strategy="semantic",
        min_tokens=12,
        target_tokens=32,
        max_tokens=64,
        overlap_tokens=0,
        semantic_breakpoint=float(selected["threshold"]),
    )
    structure_policy = semantic_policy.model_copy(
        update={"version": "structure-parent-child-v1", "strategy": "structure"}
    )
    semantic_metrics = _evaluate(
        held_out,
        policy=semantic_policy,
        embedder=embedder,
        token_counter=count_tokens,
        token_counter_id=token_counter_id,
    )
    if not embedder.model_id.startswith("st:"):
        raise RuntimeError(f"semantic embedder degraded to {embedder.model_id!r}")
    structure_metrics = _evaluate(
        held_out,
        policy=structure_policy,
        embedder=None,
        token_counter=count_tokens,
        token_counter_id=token_counter_id,
    )
    return {
        "status": "passed",
        "dataset_id": dataset["dataset_id"],
        "dataset_sha256": dataset_hash,
        "executed_at": datetime.now(UTC).isoformat(),
        "split": {"dev_documents": len(dev), "held_out_documents": len(held_out)},
        "embedding_requested": args.embedding_model,
        "embedding_revision": embedder.model_revision,
        "embedding_effective": embedder.model_id,
        "token_counter": token_counter_id,
        "dev_selection": {
            "threshold_grid": list(thresholds),
            "selected_threshold": selected["threshold"],
            "primary_metric": "boundary_balanced_accuracy",
            "held_out_not_used_for_selection": True,
        },
        "held_out": {"structure": structure_metrics, "semantic": semantic_metrics},
        "methodology": (
            "Frozen boundary-regression dataset comparing structure and semantic strategies."

        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "evaluation" / "datasets" / "chunking-cases-v1.json",
    )
    parser.add_argument("--embedding-model", default="BAAI/bge-m3")
    parser.add_argument(
        "--embedding-revision",
        help=(
            "immutable model commit SHA; the packaged revision manifest supplies the default "
            "for BAAI/bge-m3"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "evaluation" / "reports" / "chunking-report-v1.json",
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
