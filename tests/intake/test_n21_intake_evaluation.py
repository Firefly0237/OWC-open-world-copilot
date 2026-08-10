from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from owcopilot.intake.contracts import DocxParagraphLocator
from owcopilot.intake.evaluation import (
    EvaluationArtifactBytes,
    EvaluationClaimTier,
    EvaluationSealError,
    FrozenTriageEvaluationSet,
    QualityEvidenceStatus,
    TriageEvaluationManifest,
    TriageGoldSet,
    TrustedEvaluationRegistry,
    adapt_production_output,
    build_trusted_representative_seal,
    evaluate_frozen_triage,
    load_frozen_triage_evaluation_set,
    load_trusted_evaluation_registry,
)
from owcopilot.intake.triage import TriagePolicy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "n21_intake"
SYNTHETIC_FIXTURE = FIXTURES / "synthetic_triage_mechanism_v2.json"
TRUSTED_REGISTRY = FIXTURES / "trusted_evaluation_registry.json"
FROZEN_AT = datetime(2026, 8, 3, tzinfo=UTC)
PREDICTED_AT = datetime(2026, 8, 4, tzinfo=UTC)


def _fixture() -> FrozenTriageEvaluationSet:
    return load_frozen_triage_evaluation_set(SYNTHETIC_FIXTURE)


def _representative(
    evaluation_set: FrozenTriageEvaluationSet | None = None,
) -> FrozenTriageEvaluationSet:
    source = evaluation_set or _fixture()
    manifest = source.manifest.model_copy(update={"dataset_kind": "REPRESENTATIVE_SEALED"})
    return source.model_copy(update={"manifest": manifest})


def _registry_for(evaluation_set: FrozenTriageEvaluationSet) -> TrustedEvaluationRegistry:
    seal = build_trusted_representative_seal(
        evaluation_set,
        manifest_frozen_at=FROZEN_AT,
        gold_frozen_at=FROZEN_AT,
        thresholds_frozen_at=FROZEN_AT,
        predictions_frozen_at=PREDICTED_AT,
    )
    return TrustedEvaluationRegistry(
        registry_version="n21-evaluation-registry-v1",
        representative_seals=(seal,),
    )


def _with_predictions(
    evaluation_set: FrozenTriageEvaluationSet,
    predictions: tuple,
) -> FrozenTriageEvaluationSet:
    return evaluation_set.model_copy(update={"predictions": predictions})


def test_synthetic_runner_is_deterministic_but_quality_is_blocked() -> None:
    evaluation_set = _fixture()
    trusted_registry = load_trusted_evaluation_registry(TRUSTED_REGISTRY)
    first = evaluate_frozen_triage(evaluation_set, trusted_registry=trusted_registry)
    second = evaluate_frozen_triage(evaluation_set, trusted_registry=trusted_registry)

    assert first == second
    assert first.case_count == 8
    assert first.selected_count == 6
    assert first.metrics.macro_f1 == pytest.approx(37 / 60)
    assert first.metrics.selective_accuracy == pytest.approx(5 / 6)
    assert first.metrics.coverage == pytest.approx(0.75)
    assert first.metrics.locator_fidelity == 1.0
    assert first.metrics.risk_coverage_curve[0].coverage == 0.0
    assert first.metrics.risk_coverage_curve[-1].coverage == 0.75
    assert first.all_preregistered_gates_passed is True
    assert first.trusted_seal_verified is False
    assert first.quality_evidence_status is QualityEvidenceStatus.BLOCKED_EXTERNAL_DATA
    assert first.claim_tier is EvaluationClaimTier.MECHANISM_VERIFIED


def test_synthetic_metadata_and_even_a_matching_registry_cannot_upgrade_claim() -> None:
    synthetic = _fixture()
    representative_copy = _representative(synthetic)
    registry = _registry_for(representative_copy)

    # The same seal bytes are deliberately supplied with the original synthetic
    # input. Dataset kind is an absolute boundary, so the registry is ignored.
    report = evaluate_frozen_triage(synthetic, trusted_registry=registry)
    assert report.claim_tier is EvaluationClaimTier.MECHANISM_VERIFIED
    assert report.quality_evidence_status is QualityEvidenceStatus.BLOCKED_EXTERNAL_DATA
    with pytest.raises(ValueError, match="genuinely representative"):
        build_trusted_representative_seal(
            synthetic,
            manifest_frozen_at=FROZEN_AT,
            gold_frozen_at=FROZEN_AT,
            thresholds_frozen_at=FROZEN_AT,
            predictions_frozen_at=PREDICTED_AT,
        )


def test_sealed_representative_path_requires_external_registry_and_freeze_order() -> None:
    representative = _representative()

    untrusted_report = evaluate_frozen_triage(representative)
    assert untrusted_report.claim_tier is EvaluationClaimTier.MECHANISM_VERIFIED
    assert untrusted_report.quality_evidence_status is QualityEvidenceStatus.BLOCKED_EXTERNAL_DATA

    registry = _registry_for(representative)
    trusted_report = evaluate_frozen_triage(representative, trusted_registry=registry)
    assert trusted_report.trusted_seal_verified is True
    assert trusted_report.quality_evidence_status is QualityEvidenceStatus.SEALED_REPRESENTATIVE
    assert trusted_report.claim_tier is EvaluationClaimTier.QUALITY_SUPPORTED

    with pytest.raises(ValidationError, match="independently frozen before predictions"):
        build_trusted_representative_seal(
            representative,
            manifest_frozen_at=FROZEN_AT,
            gold_frozen_at=FROZEN_AT,
            thresholds_frozen_at=PREDICTED_AT,
            predictions_frozen_at=PREDICTED_AT,
        )


def test_predictions_are_rederived_from_raw_output_not_hand_filled_fields() -> None:
    evaluation_set = _fixture()
    prediction = evaluation_set.predictions[0]

    assert "predicted_role" not in type(prediction).model_fields
    assert "abstain" not in type(prediction).model_fields
    assert "confidence" not in type(prediction).model_fields

    forged_verdict = prediction.verdict.model_copy(update={"confidence": 0.94})
    forged_prediction = prediction.model_copy(update={"verdict": forged_verdict})
    forged_set = _with_predictions(
        evaluation_set,
        (forged_prediction, *evaluation_set.predictions[1:]),
    )
    with pytest.raises(ValueError, match="not produced by derive_triage_verdict"):
        evaluate_frozen_triage(forged_set)


def test_locator_fidelity_re_resolves_exact_artifact_bytes() -> None:
    evaluation_set = _fixture()
    original = evaluation_set.predictions[0]
    moved_segment = original.segment.model_copy(
        update={"locator": DocxParagraphLocator(paragraph_index=1)}
    )
    moved_prediction = adapt_production_output(
        case_id=original.case_id,
        artifact_sha256=original.artifact_sha256,
        segment=moved_segment,
        raw_output_id=original.raw_output_id,
        raw_output=original.raw_output,
        verdict_id=original.verdict.verdict_id,
        provider_name=original.provider_name,
        model_name=original.model_name,
        policy=TriagePolicy(
            version=original.policy_version,
            confidence_threshold=original.policy_confidence_threshold,
        ),
    )
    changed = evaluation_set.model_copy(
        update={
            "predictions": (moved_prediction, *evaluation_set.predictions[1:]),
        }
    )

    report = evaluate_frozen_triage(changed)
    assert report.metrics.locator_fidelity == pytest.approx(7 / 8)
    assert {gate.name: gate.passed for gate in report.gate_checks}["locator_fidelity"] is False


def test_all_abstain_fails_coverage_selection_calibration_and_quality() -> None:
    evaluation_set = _fixture()
    predictions = []
    for prediction in evaluation_set.predictions:
        predictions.append(
            adapt_production_output(
                case_id=prediction.case_id,
                artifact_sha256=prediction.artifact_sha256,
                segment=prediction.segment,
                raw_output_id=prediction.raw_output_id,
                raw_output=(
                    '{"content_role":"UNKNOWN","confidence":0.0,"evidence_spans":[]}'
                ),
                verdict_id=prediction.verdict.verdict_id,
                provider_name=prediction.provider_name,
                model_name=prediction.model_name,
                policy=TriagePolicy(
                    version=prediction.policy_version,
                    confidence_threshold=prediction.policy_confidence_threshold,
                ),
            )
        )
    changed = _with_predictions(evaluation_set, tuple(predictions))
    report = evaluate_frozen_triage(changed)
    gates = {gate.name: gate.passed for gate in report.gate_checks}

    assert report.all_abstained is True
    assert report.metrics.macro_f1 == 0.0
    assert report.metrics.selective_accuracy == 0.0
    assert report.metrics.coverage == 0.0
    assert report.metrics.risk_coverage_auc == 1.0
    assert report.metrics.expected_calibration_error == 1.0
    assert gates["nonzero_selection"] is False
    assert gates["coverage"] is False
    assert report.claim_tier is EvaluationClaimTier.MECHANISM_VERIFIED


def _mutate_thresholds(value: FrozenTriageEvaluationSet) -> FrozenTriageEvaluationSet:
    thresholds = value.thresholds.model_copy(update={"minimum_macro_f1": 0.61})
    return value.model_copy(update={"thresholds": thresholds})


def _mutate_gold(value: FrozenTriageEvaluationSet) -> FrozenTriageEvaluationSet:
    first = value.gold.annotations[0].model_copy(update={"gold_role": "REFERENCE"})
    gold = value.gold.model_copy(update={"annotations": (first, *value.gold.annotations[1:])})
    return value.model_copy(update={"gold": gold})


def _mutate_order(value: FrozenTriageEvaluationSet) -> FrozenTriageEvaluationSet:
    order = tuple(range(1, len(value.manifest.cases))) + (0,)
    manifest = value.manifest.model_copy(
        update={"cases": tuple(value.manifest.cases[index] for index in order)}
    )
    gold = value.gold.model_copy(
        update={"annotations": tuple(value.gold.annotations[index] for index in order)}
    )
    return value.model_copy(
        update={
            "manifest": manifest,
            "gold": gold,
            "predictions": tuple(value.predictions[index] for index in order),
        }
    )


def _mutate_provider(value: FrozenTriageEvaluationSet) -> FrozenTriageEvaluationSet:
    first = value.predictions[0].model_copy(update={"provider_name": "mutant-provider"})
    return _with_predictions(value, (first, *value.predictions[1:]))


def _mutate_prediction(value: FrozenTriageEvaluationSet) -> FrozenTriageEvaluationSet:
    old = value.predictions[0]
    raw = old.raw_output.replace("0.95", "0.94")
    first = adapt_production_output(
        case_id=old.case_id,
        artifact_sha256=old.artifact_sha256,
        segment=old.segment,
        raw_output_id=old.raw_output_id,
        raw_output=raw,
        verdict_id=old.verdict.verdict_id,
        provider_name=old.provider_name,
        model_name=old.model_name,
        policy=TriagePolicy(
            version=old.policy_version,
            confidence_threshold=old.policy_confidence_threshold,
        ),
    )
    return _with_predictions(value, (first, *value.predictions[1:]))


def _mutate_artifact(value: FrozenTriageEvaluationSet) -> FrozenTriageEvaluationSet:
    artifact = value.artifacts[0]
    changed = EvaluationArtifactBytes(
        artifact_version_id=artifact.artifact_version_id,
        content_bytes=artifact.content_bytes[:-1] + bytes([artifact.content_bytes[-1] ^ 1]),
    )
    return value.model_copy(update={"artifacts": (changed,)})


@pytest.mark.parametrize(
    "mutator",
    [
        _mutate_thresholds,
        _mutate_gold,
        _mutate_order,
        _mutate_provider,
        _mutate_prediction,
        _mutate_artifact,
    ],
    ids=["thresholds", "gold", "order", "provider", "prediction", "artifact"],
)
def test_any_sealed_component_mutation_fails_closed(mutator) -> None:
    representative = _representative()
    registry = _registry_for(representative)

    with pytest.raises(EvaluationSealError, match="seal mismatch"):
        evaluate_frozen_triage(mutator(representative), trusted_registry=registry)


def test_component_models_keep_manifest_gold_and_predictions_separate() -> None:
    evaluation_set = _fixture()

    assert isinstance(evaluation_set.manifest, TriageEvaluationManifest)
    assert isinstance(evaluation_set.gold, TriageGoldSet)
    assert not hasattr(evaluation_set.gold.annotations[0], "predicted_role")
    assert not hasattr(evaluation_set.predictions[0], "gold_role")
