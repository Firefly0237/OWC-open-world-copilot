"""Rule blind-spot negative control: mutation-testing specificity half of the acceptance suite.

These tests prove three things, in tandem: the injected narrative mutations are real defects
pointing at real content (``seed_blind_spots``), the deterministic audit stays SILENT on all of
them (the negative control — ``blind_spot_rate == 1.0``, zero new issues), and the control is NOT
vacuous — the very same diff harness DOES flag a structural mutation the rules can see. The last
one is what answers the "target painted around the arrow" objection: a negative control that can
never fail proves nothing.
"""

from __future__ import annotations

from pathlib import Path

from owcopilot.evaluation import (
    BlindSpotError,
    audit_open_issues,
    build_acceptance_world,
    build_blind_spot_report,
    evaluate_blind_spots,
    seed_blind_spots,
)
from owcopilot.evaluation.blind_spots import (
    EMOTIONAL_CONTINUITY_BREAK,
    MOTIVATION_SELF_CONTRADICTION,
    PERSONALITY_DRIFT,
    PROSE_SENTIMENT_VS_STRUCTURE,
    TONE_REGISTER_INCONSISTENCY,
    UNMODELED_FACT_CONTRADICTION,
)

_REF_MAPS = {
    "entity": "entities",
    "quest": "quests",
    "poi": "pois",
    "dialogue": "dialogues",
}


def _ref_exists(bundle: object, ref: str) -> bool:
    kind, _, ident = ref.partition(":")
    container = getattr(bundle, _REF_MAPS[kind])
    return ident in container


def test_seed_blind_spots_injects_six_categorized_defects_at_real_refs() -> None:
    clean = build_acceptance_world()
    mutated, spots = seed_blind_spots(clean)

    assert len(spots) == 6
    # each of the six characterized narrative-blind-spot categories appears exactly once
    assert {spot.category for spot in spots} == {
        PERSONALITY_DRIFT,
        TONE_REGISTER_INCONSISTENCY,
        MOTIVATION_SELF_CONTRADICTION,
        EMOTIONAL_CONTINUITY_BREAK,
        UNMODELED_FACT_CONTRADICTION,
        PROSE_SENTIMENT_VS_STRUCTURE,
    }
    for spot in spots:
        assert spot.contradiction.strip(), f"{spot.code} needs an editor rationale"
        assert spot.mutated_refs, f"{spot.code} must point at content"
        # every mutated ref names content that actually exists in the clean world
        for ref in spot.mutated_refs:
            assert _ref_exists(clean, ref), f"{spot.code} points at missing {ref}"

    # a pure narrative mutation: it rewrites free text, it does not add/remove content objects,
    # and it does not touch the caller's clean bundle (deep copy).
    assert set(mutated.entities) == set(clean.entities)
    assert set(mutated.quests) == set(clean.quests)
    assert clean.dialogues["dlg_quest_r2_q2"].text != mutated.dialogues["dlg_quest_r2_q2"].text


def test_blind_spots_survive_the_deterministic_audit(tmp_path: Path) -> None:
    """The negative control: every injected narrative defect is OUTSIDE the rules' expressive power,
    so the audit produces zero new issues and blind_spot_rate is 1.0 (all six survive)."""
    report = evaluate_blind_spots(build_acceptance_world(), tmp_path)

    assert report.injected == 6
    assert report.blind_spot_rate == 1.0
    assert report.new_issues == []
    assert report.caught == []
    assert len(report.survived) == 6
    assert len(report.categories) == 6
    assert all(entry["caught"] is False for entry in report.per_spot)


def test_negative_control_is_not_vacuous(tmp_path: Path) -> None:
    """A negative control that can never fail proves nothing. Feed the SAME diff harness a
    structural mutation the rules DO catch (an emptied POI purpose) and confirm it is reported as
    caught with a real new issue — so the silence on the six narrative spots is meaningful."""
    clean = build_acceptance_world()
    baseline_open = audit_open_issues(clean, tmp_path / "clean", tmp_path / "clean.sqlite")

    structural = clean.model_copy(deep=True)
    structural.pois["poi_r1_a"].purpose = ""  # fires POI_WITHOUT_NARRATIVE_PURPOSE
    mutated_open = audit_open_issues(structural, tmp_path / "struct", tmp_path / "struct.sqlite")

    probe = BlindSpotError(
        code="PROBE",
        category="structural_probe",
        mutated_refs=["poi:poi_r1_a"],
        contradiction="structural fault the rules CAN see — proves the harness can fail",
    )
    report = build_blind_spot_report(baseline_open, mutated_open, [probe])

    assert report.new_issues, "the structural mutation must surface as a new issue"
    assert any("POI_WITHOUT_NARRATIVE_PURPOSE" in item for item in report.new_issues)
    assert report.caught == ["PROBE"]
    assert report.survived == []
    assert report.blind_spot_rate == 0.0
