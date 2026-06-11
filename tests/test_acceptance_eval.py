"""Acceptance benchmark tests: world scale, seeded-error detection, retrieval and impact gates."""

from __future__ import annotations

import json
from pathlib import Path

from owcopilot.cli.main import main
from owcopilot.evaluation import (
    build_acceptance_world,
    retrieval_benchmark_queries,
    run_acceptance_evaluation,
    seed_errors,
)


def test_world_scale_meets_acceptance_targets() -> None:
    bundle = build_acceptance_world()
    assert len(bundle.entities) >= 60
    assert len(bundle.regions) == 10
    assert len(bundle.quests) >= 35
    assert len(bundle.localized_texts) >= 70
    # bilingual: every quest objective key has zh-CN and en rows
    locales_by_key: dict[str, set[str]] = {}
    for text in bundle.localized_texts.values():
        locales_by_key.setdefault(text.text_key, set()).add(text.locale)
    assert all({"zh-CN", "en"} <= locales for locales in locales_by_key.values())


def test_seeded_errors_cover_five_categories() -> None:
    _bundle, seeded = seed_errors(build_acceptance_world())
    assert len(seeded) == 25
    rules = {error.expected_rule for error in seeded}
    assert {
        "UNKNOWN_ENTITY_REF",
        "DUPLICATE_RELATION",
        "TIMELINE_VIOLATION",
        "REGION_BANNED_CONTENT_USED",
        "PLACEHOLDER_MISMATCH",
    } <= rules


def test_retrieval_benchmark_is_bilingual_30_queries() -> None:
    queries = retrieval_benchmark_queries()
    assert len(queries) == 30
    zh = [query for query, _ in queries if any("一" <= ch <= "鿿" for ch in query)]
    assert len(zh) == 15


def test_acceptance_evaluation_passes_all_gates(tmp_path: Path) -> None:
    report = run_acceptance_evaluation(tmp_path)
    failed = [check.name for check in report.checks if not check.passed]
    assert report.passed, f"failed checks: {failed}"
    assert report.metrics["detection_rate"] >= 0.85
    assert report.metrics["retrieval_hit_rate"] >= 0.90
    by_name = {check.name: check for check in report.checks}
    assert by_name["clean_world_zero_false_positives"].passed
    assert by_name["impact_recall_100"].passed
    assert by_name["qa_grounded_or_refuse"].passed


def test_cli_eval_acceptance(tmp_path: Path, capsys) -> None:
    code = main(["eval-acceptance", "--workspace", str(tmp_path / "ws")])
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["passed"] is True
    assert payload["metrics"]["seeded_errors"] == 25
