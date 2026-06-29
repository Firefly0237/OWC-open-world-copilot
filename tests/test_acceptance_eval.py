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
    # The tight-budget gate only clears when the rerank stage lifts the answer to the top.
    assert report.metrics["retrieval_tight_hit_rate"] >= 0.95
    by_name = {check.name: check for check in report.checks}
    assert by_name["clean_world_zero_false_positives"].passed
    assert by_name["impact_recall_100"].passed
    assert by_name["retrieval_tight_hit_rate_gate"].passed
    assert by_name["qa_citation_existence_or_refuse"].passed


def test_faithfulness_gate_coexists_and_skips_by_default(tmp_path: Path) -> None:
    """The opt-in entailment gate must coexist with the existence gate and, with no judge,
    skip ($0, deterministic) without dragging the overall report down."""
    report = run_acceptance_evaluation(tmp_path)
    by_name = {check.name: check for check in report.checks}
    # both QA gates present — the new one does NOT replace the existing one
    assert "qa_citation_existence_or_refuse" in by_name
    assert "qa_faithfulness_entailment" in by_name
    faith = by_name["qa_faithfulness_entailment"]
    # default (no judge) → skipped, passes vacuously, report still green
    assert faith.details["skipped"] is True
    assert faith.passed is True
    assert report.passed is True


def test_detection_rate_denominator_is_disclosed_as_a_rule_subset(tmp_path: Path) -> None:
    """detection_rate=1.0 must not be read as "all rules validated": the metrics disclose that the
    seeded errors cover only a subset of the registry, and name the uncovered rules explicitly."""
    from owcopilot.audit.default_rules import build_default_rule_registry

    report = run_acceptance_evaluation(tmp_path)
    total = len(build_default_rule_registry().codes())
    assert report.metrics["rules_total"] == total
    # the seeded world covers a strict subset of the registry — this is the honest part
    assert report.metrics["rules_covered"] < total
    uncovered = report.metrics["rules_uncovered"]
    assert len(uncovered) == total - report.metrics["rules_covered"]
    # security-relevant + dialogue-tree rules are the known-uncovered ones (covered by unit tests)
    assert "PROMPT_INJECTION" in uncovered
    assert any(code.startswith("DIALOGUE_TREE_") for code in uncovered)
    # and the gate itself carries the scope disclosure for a reader of the check
    detection_check = next(c for c in report.checks if c.name == "seeded_error_detection_gate")
    assert detection_check.details["rules_covered"] == f"{report.metrics['rules_covered']}/{total}"


def test_cli_eval_acceptance(tmp_path: Path, capsys) -> None:
    code = main(["eval-acceptance", "--workspace", str(tmp_path / "ws")])
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["passed"] is True
    assert payload["metrics"]["seeded_errors"] == 25
