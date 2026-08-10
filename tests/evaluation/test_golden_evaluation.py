from __future__ import annotations

from owcopilot.content.store import ContentStore
from owcopilot.evaluation import golden_content_bundle, run_golden_evaluation, write_golden_world


def test_golden_content_bundle_is_audit_ready() -> None:
    bundle = golden_content_bundle()

    assert "npc_aldric" in bundle.entities
    assert "location_northwatch" in bundle.entities
    assert bundle.quests["quest_missing_caravan"].localization_keys


def test_write_golden_world_round_trips_through_content_store(tmp_path) -> None:
    content_root = tmp_path / "golden"

    write_golden_world(content_root)
    loaded = ContentStore(content_root).load()

    assert loaded.entities["npc_aldric"].name == "Aldric"
    assert loaded.quests["quest_missing_caravan"].giver_npc == "npc_aldric"


def test_run_golden_evaluation_passes_all_checks(tmp_path) -> None:
    report = run_golden_evaluation(tmp_path)

    assert report.passed is True
    assert {check.name for check in report.checks} == {
        "audit_no_open_errors",
        "retrieval_has_aldric",
        "qa_citation_existence_or_refuse",
        "export_manifest_written",
        "provenance_all_approved",
    }
    assert all(check.passed for check in report.checks)
    assert report.metrics["qa_telemetry"]["calls"] == 1
    assert (tmp_path / "exports" / "generic" / "manifest.json").exists()
