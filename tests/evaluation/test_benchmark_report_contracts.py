"""Contracts for the two committed, deterministic benchmark entry-point reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.run_complexity_benchmark import build_report as build_complexity_report
from scripts.run_frozen_retrieval_diagnostics import render_report as render_retrieval_report

from owcopilot.evaluation.retrieval_dataset import FROZEN_DATASET_PATH, load_frozen_dataset

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPLEXITY_REPORT = REPO_ROOT / "benchmarks" / "complexity_corpus.md"
RETRIEVAL_REPORT = (
    REPO_ROOT / "benchmarks" / "evaluation" / "reports" / "retrieval-diagnostics-v1.json"
)


def test_complexity_report_is_fresh_and_carries_exploration_contract() -> None:
    committed = COMPLEXITY_REPORT.read_text(encoding="utf-8")

    assert committed == build_complexity_report()
    assert "states explored" in committed
    assert "outcomes reachable/total" in committed
    assert "dead branches" in committed
    assert "truncated quests" in committed
    assert "witnesses replay" in committed
    for tier in ("support(支撑级)", "chapter(章节级)", "multiline(多线级)"):
        assert f"| {tier} |" in committed


def test_frozen_retrieval_report_has_canonical_provenance_and_metric_contract() -> None:
    committed = RETRIEVAL_REPORT.read_text(encoding="utf-8")
    report: dict[str, Any] = json.loads(committed)
    dataset = load_frozen_dataset()

    # Canonical serialization plus both hashes tie the report to the exact frozen artifact.
    assert committed == render_retrieval_report(report)
    assert report["schema_version"] == "retrieval-diagnostics-v1"
    assert report["status"] == "mechanism_verified"
    assert report["dataset"]["content_digest"] == dataset.content_digest()
    assert report["dataset"]["artifact_sha256"] == hashlib.sha256(
        FROZEN_DATASET_PATH.read_bytes()
    ).hexdigest()
    assert report["dataset"]["queries"] == {
        "answerable": 104,
        "by_category": {
            "indirect": 24,
            "paraphrase": 50,
            "unanswerable": 16,
            "verbatim": 30,
        },
        "by_locale": {"en": 60, "zh-CN": 60},
        "total": 120,
        "unanswerable": 16,
    }

    # The $0 artifact is intentionally a mechanism/failure-attribution report, not a semantic KPI.
    assert report["claim_boundary"]["semantic_quality_claim"] is False
    assert report["execution"]["embedder"] == "HashingEmbedder"
    assert report["execution"]["reranker"] == "lexical"
    assert report["execution"]["model_calls"] == 0
    assert report["execution"]["network_calls"] == 0
    assert report["execution"]["runners"] == [
        "run_retrieval_recall_eval",
        "run_staged_retrieval_diagnostic",
        "run_retrieval_ab_eval",
    ]
    assert all(report["checks"].values())

    digest = dataset.content_digest()
    recall = report["recall"]
    staged = report["fp2_fp3"]
    paired = report["paired_mcnemar_ab"]
    assert recall["dataset_hash"] == staged["dataset_hash"] == paired["dataset_hash"] == digest
    assert recall["scored_queries"] == staged["scored_queries"] == 104
    assert recall["excluded_unanswerable"] == staged["excluded_unanswerable"] == 16
    assert staged["overall"]["fp3_pack_hits"] <= staged["overall"]["fp2_pool_hits"]
    assert staged["overall"]["budget_trim_loss"] >= 0

    assert paired["n_paired"] == 104
    assert paired["primary"] == paired["overall_by_k"][str(paired["primary_k"])]
    for comparison in paired["overall_by_k"].values():
        contingency = comparison["contingency"]
        mcnemar = comparison["mcnemar"]
        assert comparison["n"] == contingency["n"] == paired["n_paired"]
        assert mcnemar["b"] == contingency["a_only_hit"]
        assert mcnemar["c"] == contingency["b_only_hit"]
        assert mcnemar["method"] == "mcnemar_exact_binomial"
