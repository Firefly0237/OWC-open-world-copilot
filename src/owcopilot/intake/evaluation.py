"""Honest, seal-bound N2.1 evaluation for intake triage and source locators.

The module deliberately keeps four things separate: the ordered input manifest,
human gold annotations, preregistered thresholds, and production outputs.  A
representative quality claim additionally requires a caller-supplied trusted
registry whose seal pins every one of those components.  A fixture cannot make
itself representative by changing metadata in its own file.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, Literal, TypeVar

from pydantic import ValidationInfo, field_validator, model_validator

from ..contracts._base import _StrictFrozenModel
from .contracts import SourceLocator, SourceSegment, TriageVerdict
from .parsers import (
    IntakePolicy,
    IntakeValidationError,
    parse_source_segments,
    validate_upload_chunks,
    verify_source_segment,
)
from .triage import TriagePolicy, derive_triage_verdict

SCORER_VERSION: Final[Literal["n21-triage-eval-v2"]] = "n21-triage-eval-v2"
TRUSTED_REGISTRY_VERSION: Final[Literal["n21-evaluation-registry-v1"]] = (
    "n21-evaluation-registry-v1"
)
MINIMUM_PREREGISTERED_COVERAGE: Final = 0.50

ContentRole = Literal["CANON_CANDIDATE", "REFERENCE", "MIXED", "UNKNOWN"]
DatasetKind = Literal["SYNTHETIC_FIXTURE", "REPRESENTATIVE_SEALED"]
GateComparison = Literal[">=", "<="]

CONTENT_ROLES: tuple[ContentRole, ...] = (
    "CANON_CANDIDATE",
    "REFERENCE",
    "MIXED",
    "UNKNOWN",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", flags=re.ASCII)
_ModelT = TypeVar("_ModelT", bound=_StrictFrozenModel)


class EvaluationSealError(ValueError):
    """A representative input differs from its independently trusted seal."""


def _exact_text(value: object, *, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a non-empty string without coercion")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} contains a lone Unicode surrogate") from exc
    return value


def _sha256(value: object, *, label: str) -> str:
    text = _exact_text(value, label=label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _probability(value: object, *, label: str) -> float:
    if type(value) is not float or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{label} must be a finite float in [0, 1] without coercion")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_json(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _model_digest(model: _StrictFrozenModel) -> str:
    return _digest_json(model.model_dump(mode="json"))


def _models_digest(models: Sequence[_StrictFrozenModel]) -> str:
    return _digest_json([model.model_dump(mode="json") for model in models])


class EvaluationClaimTier(StrEnum):
    MECHANISM_VERIFIED = "MECHANISM_VERIFIED"
    QUALITY_SUPPORTED = "QUALITY_SUPPORTED"


class QualityEvidenceStatus(StrEnum):
    """Whether independently trusted representative evidence is available."""

    BLOCKED_EXTERNAL_DATA = "BLOCKED_EXTERNAL_DATA"
    SEALED_REPRESENTATIVE = "SEALED_REPRESENTATIVE"


class TriageQualityThresholds(_StrictFrozenModel):
    """Quality gates that must be frozen before any prediction is produced."""

    minimum_macro_f1: float
    minimum_selective_accuracy: float
    minimum_coverage: float
    maximum_risk_coverage_auc: float
    maximum_expected_calibration_error: float
    minimum_locator_fidelity: float
    calibration_bin_count: int

    @field_validator(
        "minimum_macro_f1",
        "minimum_selective_accuracy",
        "minimum_coverage",
        "maximum_risk_coverage_auc",
        "maximum_expected_calibration_error",
        "minimum_locator_fidelity",
        mode="before",
    )
    @classmethod
    def validate_probabilities(cls, value: object, info: ValidationInfo) -> float:
        return _probability(value, label=info.field_name or "threshold")

    @field_validator("minimum_coverage")
    @classmethod
    def require_positive_coverage_floor(cls, value: float) -> float:
        if value < MINIMUM_PREREGISTERED_COVERAGE:
            raise ValueError(
                "minimum_coverage must be preregistered at or above the formal 0.50 floor"
            )
        return value

    @field_validator("calibration_bin_count", mode="before")
    @classmethod
    def validate_bin_count(cls, value: object) -> int:
        if type(value) is not int or not 2 <= value <= 100:
            raise ValueError("calibration_bin_count must be an integer in [2, 100]")
        return value


class EvaluationArtifactDescriptor(_StrictFrozenModel):
    """Immutable artifact identity in the preregistered input manifest."""

    artifact_version_id: str
    original_filename: str
    mime_type: str
    byte_size: int
    content_sha256: str

    @field_validator("artifact_version_id", "original_filename", "mime_type", mode="before")
    @classmethod
    def validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _exact_text(value, label=info.field_name or "artifact field")

    @field_validator("byte_size", mode="before")
    @classmethod
    def validate_size(cls, value: object) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("byte_size must be a positive integer without coercion")
        return value

    @field_validator("content_sha256", mode="before")
    @classmethod
    def validate_hash(cls, value: object) -> str:
        return _sha256(value, label="content_sha256")


class EvaluationCaseBinding(_StrictFrozenModel):
    """Ordered binding from one case to exact artifact and segment identities."""

    case_id: str
    artifact_version_id: str
    artifact_sha256: str
    segment_id: str
    segment_text_hash: str
    expected_locator: SourceLocator

    @field_validator("case_id", "artifact_version_id", "segment_id", mode="before")
    @classmethod
    def validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _exact_text(value, label=info.field_name or "case binding")

    @field_validator("artifact_sha256", "segment_text_hash", mode="before")
    @classmethod
    def validate_hashes(cls, value: object, info: ValidationInfo) -> str:
        return _sha256(value, label=info.field_name or "binding hash")


class TriageEvaluationManifest(_StrictFrozenModel):
    """The artifact set and case order frozen independently of gold and outputs."""

    dataset_id: str
    dataset_version: str
    dataset_kind: DatasetKind
    scorer_version: Literal["n21-triage-eval-v2"]
    artifacts: tuple[EvaluationArtifactDescriptor, ...]
    cases: tuple[EvaluationCaseBinding, ...]

    @field_validator("dataset_id", "dataset_version", mode="before")
    @classmethod
    def validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _exact_text(value, label=info.field_name or "manifest field")

    @model_validator(mode="after")
    def validate_manifest(self) -> TriageEvaluationManifest:
        if not self.artifacts or not self.cases:
            raise ValueError("evaluation manifest must contain artifacts and cases")
        artifact_ids = [artifact.artifact_version_id for artifact in self.artifacts]
        artifact_hashes = [artifact.content_sha256 for artifact in self.artifacts]
        case_ids = [case.case_id for case in self.cases]
        segment_ids = [case.segment_id for case in self.cases]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact_version_id values must be unique")
        if len(artifact_hashes) != len(set(artifact_hashes)):
            raise ValueError("artifact content hashes must be unique")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id values must be unique")
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("segment_id values must be unique")
        artifact_by_id = {artifact.artifact_version_id: artifact for artifact in self.artifacts}
        for case in self.cases:
            artifact = artifact_by_id.get(case.artifact_version_id)
            if artifact is None or artifact.content_sha256 != case.artifact_sha256:
                raise ValueError("case artifact binding differs from the manifest")
        return self


class TriageGoldAnnotation(_StrictFrozenModel):
    """One human gold label; predictions are intentionally absent."""

    case_id: str
    gold_role: ContentRole

    @field_validator("case_id", mode="before")
    @classmethod
    def validate_case_id(cls, value: object) -> str:
        return _exact_text(value, label="case_id")


class TriageGoldSet(_StrictFrozenModel):
    dataset_id: str
    dataset_version: str
    annotations: tuple[TriageGoldAnnotation, ...]

    @field_validator("dataset_id", "dataset_version", mode="before")
    @classmethod
    def validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _exact_text(value, label=info.field_name or "gold field")

    @model_validator(mode="after")
    def validate_annotations(self) -> TriageGoldSet:
        if not self.annotations:
            raise ValueError("gold annotation set must not be empty")
        ids = [annotation.case_id for annotation in self.annotations]
        if len(ids) != len(set(ids)):
            raise ValueError("gold case_id values must be unique")
        return self


class EvaluationArtifactBytes(_StrictFrozenModel):
    """Exact artifact bytes supplied separately from their manifest descriptors."""

    artifact_version_id: str
    content_bytes: bytes

    @field_validator("artifact_version_id", mode="before")
    @classmethod
    def validate_id(cls, value: object) -> str:
        return _exact_text(value, label="artifact_version_id")

    @field_validator("content_bytes", mode="before")
    @classmethod
    def validate_bytes(cls, value: object) -> bytes:
        if type(value) is not bytes or not value:
            raise ValueError("content_bytes must be non-empty exact bytes")
        return value


class ProductionTriagePrediction(_StrictFrozenModel):
    """Adapter output that retains raw output and its deterministically derived verdict.

    There are no writable ``predicted_role``, ``abstain``, ``confidence``, or evidence
    fields.  The evaluator re-runs :func:`derive_triage_verdict` and requires the
    persisted verdict to match exactly.
    """

    case_id: str
    artifact_version_id: str
    artifact_sha256: str
    segment: SourceSegment
    raw_output_id: str
    raw_output: str
    raw_output_sha256: str
    verdict: TriageVerdict
    provider_name: str
    model_name: str
    policy_version: str
    policy_confidence_threshold: float

    @field_validator(
        "case_id",
        "artifact_version_id",
        "raw_output_id",
        "provider_name",
        "model_name",
        "policy_version",
        mode="before",
    )
    @classmethod
    def validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _exact_text(value, label=info.field_name or "prediction field")

    @field_validator("raw_output", mode="before")
    @classmethod
    def validate_raw_output(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("raw_output must be an exact string")
        value.encode("utf-8", errors="strict")
        return value

    @field_validator("artifact_sha256", "raw_output_sha256", mode="before")
    @classmethod
    def validate_hashes(cls, value: object, info: ValidationInfo) -> str:
        return _sha256(value, label=info.field_name or "prediction hash")

    @field_validator("policy_confidence_threshold", mode="before")
    @classmethod
    def validate_policy_threshold(cls, value: object) -> float:
        return _probability(value, label="policy_confidence_threshold")

    @model_validator(mode="after")
    def validate_static_bindings(self) -> ProductionTriagePrediction:
        if self.segment.artifact_version_id != self.artifact_version_id:
            raise ValueError("prediction segment has a different artifact_version_id")
        expected_raw_hash = hashlib.sha256(self.raw_output.encode("utf-8")).hexdigest()
        if self.raw_output_sha256 != expected_raw_hash:
            raise ValueError("raw_output_sha256 does not match exact raw output")
        if self.verdict.raw_output_id != self.raw_output_id:
            raise ValueError("verdict and raw output identifiers disagree")
        if self.verdict.segment_id != self.segment.segment_id:
            raise ValueError("verdict and source segment identifiers disagree")
        if self.verdict.derivation_policy_version != self.policy_version:
            raise ValueError("verdict and prediction policy versions disagree")
        if self.verdict.policy_threshold != self.policy_confidence_threshold:
            raise ValueError("verdict and prediction policy thresholds disagree")
        return self


def adapt_production_output(
    *,
    case_id: str,
    artifact_sha256: str,
    segment: SourceSegment,
    raw_output_id: str,
    raw_output: str,
    verdict_id: str,
    provider_name: str,
    model_name: str,
    policy: TriagePolicy,
) -> ProductionTriagePrediction:
    """Create an evaluation prediction only through the real triage derivation path."""

    checked_segment = SourceSegment.model_validate(segment)
    verdict = derive_triage_verdict(
        raw_output_id=raw_output_id,
        raw_output=raw_output,
        segment=checked_segment,
        policy=policy,
        verdict_id=verdict_id,
    )
    return ProductionTriagePrediction(
        case_id=case_id,
        artifact_version_id=checked_segment.artifact_version_id,
        artifact_sha256=artifact_sha256,
        segment=checked_segment,
        raw_output_id=raw_output_id,
        raw_output=raw_output,
        raw_output_sha256=hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
        verdict=verdict,
        provider_name=provider_name,
        model_name=model_name,
        policy_version=policy.version,
        policy_confidence_threshold=policy.confidence_threshold,
    )


class FrozenTriageEvaluationSet(_StrictFrozenModel):
    """Four independently hashable inputs plus exact artifact bytes."""

    manifest: TriageEvaluationManifest
    gold: TriageGoldSet
    thresholds: TriageQualityThresholds
    predictions: tuple[ProductionTriagePrediction, ...]
    artifacts: tuple[EvaluationArtifactBytes, ...]

    @model_validator(mode="after")
    def validate_component_bindings(self) -> FrozenTriageEvaluationSet:
        case_ids = tuple(case.case_id for case in self.manifest.cases)
        if tuple(annotation.case_id for annotation in self.gold.annotations) != case_ids:
            raise ValueError("gold annotations must follow the exact frozen manifest order")
        if tuple(prediction.case_id for prediction in self.predictions) != case_ids:
            raise ValueError("predictions must follow the exact frozen manifest order")
        if self.gold.dataset_id != self.manifest.dataset_id:
            raise ValueError("gold dataset_id differs from manifest")
        if self.gold.dataset_version != self.manifest.dataset_version:
            raise ValueError("gold dataset_version differs from manifest")
        artifact_ids = tuple(artifact.artifact_version_id for artifact in self.artifacts)
        manifest_artifact_ids = tuple(
            artifact.artifact_version_id for artifact in self.manifest.artifacts
        )
        if artifact_ids != manifest_artifact_ids:
            raise ValueError("artifact bytes must follow the exact frozen manifest order")
        return self


class TrustedRepresentativeSeal(_StrictFrozenModel):
    """Trusted outer-layer record, never read from the evaluated dataset itself."""

    dataset_id: str
    dataset_version: str
    scorer_version: Literal["n21-triage-eval-v2"]
    manifest_sha256: str
    gold_sha256: str
    thresholds_sha256: str
    ordered_case_ids_sha256: str
    artifact_set_sha256: str
    provider_binding_sha256: str
    predictions_sha256: str
    manifest_frozen_at: datetime
    gold_frozen_at: datetime
    thresholds_frozen_at: datetime
    predictions_frozen_at: datetime

    @field_validator("dataset_id", "dataset_version", mode="before")
    @classmethod
    def validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _exact_text(value, label=info.field_name or "seal field")

    @field_validator(
        "manifest_sha256",
        "gold_sha256",
        "thresholds_sha256",
        "ordered_case_ids_sha256",
        "artifact_set_sha256",
        "provider_binding_sha256",
        "predictions_sha256",
        mode="before",
    )
    @classmethod
    def validate_hashes(cls, value: object, info: ValidationInfo) -> str:
        return _sha256(value, label=info.field_name or "seal hash")

    @field_validator(
        "manifest_frozen_at",
        "gold_frozen_at",
        "thresholds_frozen_at",
        "predictions_frozen_at",
    )
    @classmethod
    def validate_utc(cls, value: datetime, info: ValidationInfo) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError(f"{info.field_name} must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def validate_freeze_order(self) -> TrustedRepresentativeSeal:
        preregistered = max(
            self.manifest_frozen_at,
            self.gold_frozen_at,
            self.thresholds_frozen_at,
        )
        if preregistered >= self.predictions_frozen_at:
            raise ValueError(
                "manifest, gold, and thresholds must be independently frozen before predictions"
            )
        return self


class TrustedEvaluationRegistry(_StrictFrozenModel):
    registry_version: Literal["n21-evaluation-registry-v1"]
    representative_seals: tuple[TrustedRepresentativeSeal, ...]

    @model_validator(mode="after")
    def validate_unique_entries(self) -> TrustedEvaluationRegistry:
        keys = [(seal.dataset_id, seal.dataset_version) for seal in self.representative_seals]
        if len(keys) != len(set(keys)):
            raise ValueError("trusted registry dataset/version entries must be unique")
        return self


def compute_manifest_sha256(manifest: TriageEvaluationManifest) -> str:
    return _model_digest(TriageEvaluationManifest.model_validate(manifest))


def compute_gold_sha256(gold: TriageGoldSet) -> str:
    return _model_digest(TriageGoldSet.model_validate(gold))


def compute_thresholds_sha256(thresholds: TriageQualityThresholds) -> str:
    return _model_digest(TriageQualityThresholds.model_validate(thresholds))


def compute_predictions_sha256(predictions: Sequence[ProductionTriagePrediction]) -> str:
    checked = tuple(ProductionTriagePrediction.model_validate(item) for item in predictions)
    return _models_digest(checked)


def compute_ordered_case_ids_sha256(evaluation_set: FrozenTriageEvaluationSet) -> str:
    return _digest_json([case.case_id for case in evaluation_set.manifest.cases])


def compute_artifact_set_sha256(evaluation_set: FrozenTriageEvaluationSet) -> str:
    """Hash actual bytes and order, rather than trusting hashes declared by the input."""

    payload = [
        {
            "artifact_version_id": artifact.artifact_version_id,
            "byte_size": len(artifact.content_bytes),
            "content_sha256": hashlib.sha256(artifact.content_bytes).hexdigest(),
        }
        for artifact in evaluation_set.artifacts
    ]
    return _digest_json(payload)


def compute_provider_binding_sha256(evaluation_set: FrozenTriageEvaluationSet) -> str:
    payload = [
        {
            "case_id": prediction.case_id,
            "provider_name": prediction.provider_name,
            "model_name": prediction.model_name,
            "policy_version": prediction.policy_version,
            "policy_confidence_threshold": prediction.policy_confidence_threshold,
        }
        for prediction in evaluation_set.predictions
    ]
    return _digest_json(payload)


def build_trusted_representative_seal(
    evaluation_set: FrozenTriageEvaluationSet,
    *,
    manifest_frozen_at: datetime,
    gold_frozen_at: datetime,
    thresholds_frozen_at: datetime,
    predictions_frozen_at: datetime,
) -> TrustedRepresentativeSeal:
    """Administrative helper for the trusted registry owner, not an input self-seal."""

    checked = FrozenTriageEvaluationSet.model_validate(evaluation_set)
    if checked.manifest.dataset_kind != "REPRESENTATIVE_SEALED":
        raise ValueError("only a genuinely representative dataset may enter the trusted registry")
    return TrustedRepresentativeSeal(
        dataset_id=checked.manifest.dataset_id,
        dataset_version=checked.manifest.dataset_version,
        scorer_version=SCORER_VERSION,
        manifest_sha256=compute_manifest_sha256(checked.manifest),
        gold_sha256=compute_gold_sha256(checked.gold),
        thresholds_sha256=compute_thresholds_sha256(checked.thresholds),
        ordered_case_ids_sha256=compute_ordered_case_ids_sha256(checked),
        artifact_set_sha256=compute_artifact_set_sha256(checked),
        provider_binding_sha256=compute_provider_binding_sha256(checked),
        predictions_sha256=compute_predictions_sha256(checked.predictions),
        manifest_frozen_at=manifest_frozen_at,
        gold_frozen_at=gold_frozen_at,
        thresholds_frozen_at=thresholds_frozen_at,
        predictions_frozen_at=predictions_frozen_at,
    )


def verify_trusted_representative_seal(
    evaluation_set: FrozenTriageEvaluationSet,
    registry: TrustedEvaluationRegistry,
) -> TrustedRepresentativeSeal:
    """Fail closed if any independently sealed component has changed."""

    if evaluation_set.manifest.dataset_kind != "REPRESENTATIVE_SEALED":
        raise EvaluationSealError("synthetic data is ineligible for a representative seal")
    key = (evaluation_set.manifest.dataset_id, evaluation_set.manifest.dataset_version)
    matching = [
        seal
        for seal in registry.representative_seals
        if (seal.dataset_id, seal.dataset_version) == key
    ]
    if len(matching) != 1:
        raise EvaluationSealError("trusted registry has no unique seal for this dataset version")
    seal = matching[0]
    observed = {
        "manifest_sha256": compute_manifest_sha256(evaluation_set.manifest),
        "gold_sha256": compute_gold_sha256(evaluation_set.gold),
        "thresholds_sha256": compute_thresholds_sha256(evaluation_set.thresholds),
        "ordered_case_ids_sha256": compute_ordered_case_ids_sha256(evaluation_set),
        "artifact_set_sha256": compute_artifact_set_sha256(evaluation_set),
        "provider_binding_sha256": compute_provider_binding_sha256(evaluation_set),
        "predictions_sha256": compute_predictions_sha256(evaluation_set.predictions),
    }
    for field_name, digest in observed.items():
        if getattr(seal, field_name) != digest:
            raise EvaluationSealError(f"trusted representative seal mismatch: {field_name}")
    return seal


class RiskCoveragePoint(_StrictFrozenModel):
    coverage: float
    risk: float
    confidence_floor: float


class CalibrationBin(_StrictFrozenModel):
    index: int
    lower_bound: float
    upper_bound: float
    count: int
    mean_confidence: float
    accuracy: float
    weighted_error: float


class TriageEvaluationMetrics(_StrictFrozenModel):
    """Metrics with explicit selective-prediction semantics.

    ``macro_f1`` treats an abstention as no class prediction: it contributes one
    false negative to its gold class and no false positive. ``selective_accuracy``
    and ECE use only non-abstained cases. ``coverage`` is selected/total.
    Risk-coverage integrates the confidence-ranked selected curve with the
    trapezoid rule and assigns maximum risk to the uncovered interval. Locator
    fidelity is the fraction whose segment locator and text re-resolve from exact
    artifact bytes.
    """

    macro_f1: float
    selective_accuracy: float
    coverage: float
    risk_coverage_auc: float
    expected_calibration_error: float
    locator_fidelity: float
    risk_coverage_curve: tuple[RiskCoveragePoint, ...]
    calibration_bins: tuple[CalibrationBin, ...]


class QualityGateCheck(_StrictFrozenModel):
    name: str
    observed: float
    comparison: GateComparison
    threshold: float
    passed: bool


class TriageEvaluationReport(_StrictFrozenModel):
    scorer_version: Literal["n21-triage-eval-v2"]
    dataset_id: str
    dataset_version: str
    dataset_kind: DatasetKind
    case_count: int
    selected_count: int
    all_abstained: bool
    trusted_seal_verified: bool
    quality_evidence_status: QualityEvidenceStatus
    metrics: TriageEvaluationMetrics
    gate_checks: tuple[QualityGateCheck, ...]
    all_preregistered_gates_passed: bool
    claim_tier: EvaluationClaimTier

    @model_validator(mode="after")
    def validate_claim_boundary(self) -> TriageEvaluationReport:
        if self.case_count <= 0 or not 0 <= self.selected_count <= self.case_count:
            raise ValueError("evaluation report case counts are inconsistent")
        if self.all_abstained != (self.selected_count == 0):
            raise ValueError("all_abstained must exactly reflect selected_count")
        observed_all_passed = bool(self.gate_checks) and all(
            gate.passed for gate in self.gate_checks
        )
        if self.all_preregistered_gates_passed is not observed_all_passed:
            raise ValueError("all_preregistered_gates_passed disagrees with gate_checks")
        expected_status = (
            QualityEvidenceStatus.SEALED_REPRESENTATIVE
            if self.trusted_seal_verified
            else QualityEvidenceStatus.BLOCKED_EXTERNAL_DATA
        )
        if self.quality_evidence_status is not expected_status:
            raise ValueError("quality evidence status disagrees with trusted seal state")
        expected_tier = (
            EvaluationClaimTier.QUALITY_SUPPORTED
            if self.trusted_seal_verified and observed_all_passed
            else EvaluationClaimTier.MECHANISM_VERIFIED
        )
        if self.claim_tier is not expected_tier:
            raise ValueError("claim_tier exceeds trusted representative evidence")
        if self.dataset_kind == "SYNTHETIC_FIXTURE" and self.trusted_seal_verified:
            raise ValueError("synthetic datasets can never verify a representative seal")
        return self


def evaluate_frozen_triage(
    evaluation_set: FrozenTriageEvaluationSet,
    *,
    trusted_registry: TrustedEvaluationRegistry | None = None,
    intake_policy: IntakePolicy | None = None,
) -> TriageEvaluationReport:
    """Evaluate production-derived outputs and issue only evidence-bounded claims."""

    checked = FrozenTriageEvaluationSet.model_validate(evaluation_set)
    seal_verified = False
    if checked.manifest.dataset_kind == "REPRESENTATIVE_SEALED":
        if trusted_registry is not None:
            verify_trusted_representative_seal(checked, trusted_registry)
            seal_verified = True
    # A registry supplied alongside synthetic input is intentionally ignored: the
    # immutable dataset kind is an absolute quality-claim boundary.
    locator_results = _validate_production_bindings_and_locators(
        checked,
        intake_policy=intake_policy or IntakePolicy(),
    )
    pairs = tuple(zip(checked.gold.annotations, checked.predictions, strict=True))
    selected = tuple(pair for pair in pairs if not pair[1].verdict.abstain)
    curve = _risk_coverage_curve(pairs)
    ece, bins = _expected_calibration_error(
        selected,
        bin_count=checked.thresholds.calibration_bin_count,
    )
    metrics = TriageEvaluationMetrics(
        macro_f1=_macro_f1(pairs),
        selective_accuracy=_selective_accuracy(selected),
        coverage=len(selected) / len(pairs),
        risk_coverage_auc=_risk_coverage_auc(curve),
        expected_calibration_error=ece,
        locator_fidelity=sum(locator_results) / len(locator_results),
        risk_coverage_curve=curve,
        calibration_bins=bins,
    )
    gates = _gate_checks(metrics, checked.thresholds, selected_count=len(selected))
    all_gates_passed = all(gate.passed for gate in gates)
    claim_tier = (
        EvaluationClaimTier.QUALITY_SUPPORTED
        if seal_verified and all_gates_passed
        else EvaluationClaimTier.MECHANISM_VERIFIED
    )
    return TriageEvaluationReport(
        scorer_version=SCORER_VERSION,
        dataset_id=checked.manifest.dataset_id,
        dataset_version=checked.manifest.dataset_version,
        dataset_kind=checked.manifest.dataset_kind,
        case_count=len(pairs),
        selected_count=len(selected),
        all_abstained=not selected,
        trusted_seal_verified=seal_verified,
        quality_evidence_status=(
            QualityEvidenceStatus.SEALED_REPRESENTATIVE
            if seal_verified
            else QualityEvidenceStatus.BLOCKED_EXTERNAL_DATA
        ),
        metrics=metrics,
        gate_checks=gates,
        all_preregistered_gates_passed=all_gates_passed,
        claim_tier=claim_tier,
    )


def _validate_production_bindings_and_locators(
    evaluation_set: FrozenTriageEvaluationSet,
    *,
    intake_policy: IntakePolicy,
) -> tuple[bool, ...]:
    descriptors = {
        descriptor.artifact_version_id: descriptor
        for descriptor in evaluation_set.manifest.artifacts
    }
    bytes_by_id = {
        artifact.artifact_version_id: artifact.content_bytes
        for artifact in evaluation_set.artifacts
    }
    uploads = {}
    for artifact_id, descriptor in descriptors.items():
        content = bytes_by_id[artifact_id]
        if len(content) != descriptor.byte_size:
            raise ValueError("artifact byte_size differs from exact supplied bytes")
        if hashlib.sha256(content).hexdigest() != descriptor.content_sha256:
            raise ValueError("artifact content hash differs from exact supplied bytes")
        uploads[artifact_id] = validate_upload_chunks(
            [content],
            original_filename=descriptor.original_filename,
            mime_type=descriptor.mime_type,
            policy=intake_policy,
        )

    results: list[bool] = []
    for binding, prediction in zip(
        evaluation_set.manifest.cases,
        evaluation_set.predictions,
        strict=True,
    ):
        if (
            prediction.artifact_version_id != binding.artifact_version_id
            or prediction.artifact_sha256 != binding.artifact_sha256
            or prediction.segment.segment_id != binding.segment_id
            or prediction.segment.text_hash != binding.segment_text_hash
        ):
            raise ValueError("production prediction differs from the frozen case binding")
        expected = derive_triage_verdict(
            raw_output_id=prediction.raw_output_id,
            raw_output=prediction.raw_output,
            segment=prediction.segment,
            policy=TriagePolicy(
                version=prediction.policy_version,
                confidence_threshold=prediction.policy_confidence_threshold,
            ),
            verdict_id=prediction.verdict.verdict_id,
        )
        if expected != prediction.verdict:
            raise ValueError("prediction verdict was not produced by derive_triage_verdict")
        try:
            verify_source_segment(
                uploads[prediction.artifact_version_id],
                prediction.segment,
            )
        except IntakeValidationError:
            results.append(False)
        else:
            results.append(prediction.segment.locator == binding.expected_locator)
    return tuple(results)


_Pair = tuple[TriageGoldAnnotation, ProductionTriagePrediction]


def _macro_f1(pairs: Sequence[_Pair]) -> float:
    scores: list[float] = []
    for role in CONTENT_ROLES:
        true_positive = sum(
            not prediction.verdict.abstain
            and gold.gold_role == role
            and prediction.verdict.content_role == role
            for gold, prediction in pairs
        )
        false_positive = sum(
            not prediction.verdict.abstain
            and gold.gold_role != role
            and prediction.verdict.content_role == role
            for gold, prediction in pairs
        )
        false_negative = sum(
            gold.gold_role == role
            and (prediction.verdict.abstain or prediction.verdict.content_role != role)
            for gold, prediction in pairs
        )
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(0.0 if denominator == 0 else 2 * true_positive / denominator)
    return sum(scores) / len(scores)


def _selective_accuracy(selected: Sequence[_Pair]) -> float:
    if not selected:
        return 0.0
    return sum(
        prediction.verdict.content_role == gold.gold_role for gold, prediction in selected
    ) / len(selected)


def _risk_coverage_curve(pairs: Sequence[_Pair]) -> tuple[RiskCoveragePoint, ...]:
    selected = [pair for pair in pairs if not pair[1].verdict.abstain]
    selected.sort(key=lambda pair: (-pair[1].verdict.confidence, pair[0].case_id))
    errors = 0
    points = [RiskCoveragePoint(coverage=0.0, risk=0.0, confidence_floor=1.0)]
    for rank, (gold, prediction) in enumerate(selected, start=1):
        errors += prediction.verdict.content_role != gold.gold_role
        points.append(
            RiskCoveragePoint(
                coverage=rank / len(pairs),
                risk=errors / rank,
                confidence_floor=prediction.verdict.confidence,
            )
        )
    return tuple(points)


def _risk_coverage_auc(curve: Sequence[RiskCoveragePoint]) -> float:
    area = 0.0
    for left, right in pairwise(curve):
        area += (right.coverage - left.coverage) * (left.risk + right.risk) / 2
    final_coverage = curve[-1].coverage
    area += 1.0 - final_coverage  # maximum-risk penalty for the uncovered interval
    return area


def _expected_calibration_error(
    selected: Sequence[_Pair],
    *,
    bin_count: int,
) -> tuple[float, tuple[CalibrationBin, ...]]:
    members: list[list[_Pair]] = [[] for _ in range(bin_count)]
    for pair in selected:
        confidence = pair[1].verdict.confidence
        index = min(int(confidence * bin_count), bin_count - 1)
        members[index].append(pair)

    bins: list[CalibrationBin] = []
    ece = 1.0 if not selected else 0.0
    for index, observed in enumerate(members):
        count = len(observed)
        if count:
            mean_confidence = sum(pair[1].verdict.confidence for pair in observed) / count
            accuracy = sum(
                prediction.verdict.content_role == gold.gold_role
                for gold, prediction in observed
            ) / count
            weighted_error = count / len(selected) * abs(accuracy - mean_confidence)
        else:
            mean_confidence = 0.0
            accuracy = 0.0
            weighted_error = 0.0
        if selected:
            ece += weighted_error
        bins.append(
            CalibrationBin(
                index=index,
                lower_bound=index / bin_count,
                upper_bound=(index + 1) / bin_count,
                count=count,
                mean_confidence=mean_confidence,
                accuracy=accuracy,
                weighted_error=weighted_error,
            )
        )
    return ece, tuple(bins)


def _gate_checks(
    metrics: TriageEvaluationMetrics,
    thresholds: TriageQualityThresholds,
    *,
    selected_count: int,
) -> tuple[QualityGateCheck, ...]:
    checks: tuple[tuple[str, float, GateComparison, float], ...] = (
        ("nonzero_selection", float(selected_count), ">=", 1.0),
        ("macro_f1", metrics.macro_f1, ">=", thresholds.minimum_macro_f1),
        (
            "selective_accuracy",
            metrics.selective_accuracy,
            ">=",
            thresholds.minimum_selective_accuracy,
        ),
        ("coverage", metrics.coverage, ">=", thresholds.minimum_coverage),
        (
            "risk_coverage_auc",
            metrics.risk_coverage_auc,
            "<=",
            thresholds.maximum_risk_coverage_auc,
        ),
        (
            "expected_calibration_error",
            metrics.expected_calibration_error,
            "<=",
            thresholds.maximum_expected_calibration_error,
        ),
        (
            "locator_fidelity",
            metrics.locator_fidelity,
            ">=",
            thresholds.minimum_locator_fidelity,
        ),
    )
    return tuple(
        QualityGateCheck(
            name=name,
            observed=observed,
            comparison=comparison,
            threshold=threshold,
            passed=(observed >= threshold if comparison == ">=" else observed <= threshold),
        )
        for name, observed, comparison, threshold in checks
    )


def _strict_json(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8", errors="strict")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("evaluation JSON contains a duplicate key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    if type(value) is not dict:
        raise ValueError("evaluation JSON must contain one object")
    return value


def _json_model(model_type: type[_ModelT], value: object) -> _ModelT:
    return model_type.model_validate_json(_canonical_json_bytes(value))


def load_frozen_triage_evaluation_set(path: str | Path) -> FrozenTriageEvaluationSet:
    """Load the repository's combined synthetic mechanism fixture.

    Representative data intentionally has no combined self-sealing loader.  Its
    manifest, gold, thresholds, outputs, artifact bytes, and trusted registry must
    be supplied by their independently controlled owners.
    """

    payload = _strict_json(path)
    metadata = payload.get("metadata")
    raw_artifact = payload.get("artifact")
    if type(metadata) is not dict or type(raw_artifact) is not dict:
        raise ValueError("fixture metadata and artifact must be objects")
    if metadata.get("dataset_kind") != "SYNTHETIC_FIXTURE":
        raise ValueError("combined fixture loader accepts synthetic mechanism data only")
    dataset_id = _exact_text(metadata.get("dataset_id"), label="dataset_id")
    dataset_version = _exact_text(metadata.get("dataset_version"), label="dataset_version")
    if metadata.get("scorer_version") != SCORER_VERSION:
        raise ValueError("fixture scorer_version differs from the active scorer")
    artifact_id = _exact_text(
        raw_artifact.get("artifact_version_id"), label="artifact_version_id"
    )
    filename = _exact_text(raw_artifact.get("original_filename"), label="original_filename")
    mime_type = _exact_text(raw_artifact.get("mime_type"), label="mime_type")
    encoded = raw_artifact.get("content_base64")
    if type(encoded) is not str:
        raise ValueError("artifact content_base64 must be a string")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("artifact content_base64 is invalid") from exc
    upload = validate_upload_chunks(
        [content],
        original_filename=filename,
        mime_type=mime_type,
        policy=IntakePolicy(),
    )
    parsed_segments = parse_source_segments(upload, artifact_version_id=artifact_id)
    artifact_hash = hashlib.sha256(content).hexdigest()

    raw_gold = payload.get("gold_annotations")
    if type(raw_gold) is not list:
        raise ValueError("fixture gold_annotations must be an array")
    gold = TriageGoldSet(
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        annotations=tuple(_json_model(TriageGoldAnnotation, item) for item in raw_gold),
    )
    thresholds = _json_model(TriageQualityThresholds, payload.get("thresholds"))
    raw_predictions = payload.get("production_outputs")
    if type(raw_predictions) is not list:
        raise ValueError("fixture production_outputs must be an array")
    predictions: list[ProductionTriagePrediction] = []
    bindings: list[EvaluationCaseBinding] = []
    for raw in raw_predictions:
        if type(raw) is not dict:
            raise ValueError("each production output must be an object")
        ordinal = raw.get("segment_ordinal")
        if type(ordinal) is not int or not 0 <= ordinal < len(parsed_segments):
            raise ValueError("segment_ordinal must identify one parsed source segment")
        segment = parsed_segments[ordinal]
        case_id = _exact_text(raw.get("case_id"), label="case_id")
        policy = TriagePolicy(
            version=_exact_text(raw.get("policy_version"), label="policy_version"),
            confidence_threshold=_probability(
                raw.get("policy_confidence_threshold"),
                label="policy_confidence_threshold",
            ),
        )
        predictions.append(
            adapt_production_output(
                case_id=case_id,
                artifact_sha256=artifact_hash,
                segment=segment,
                raw_output_id=_exact_text(raw.get("raw_output_id"), label="raw_output_id"),
                raw_output=_exact_text(raw.get("raw_output"), label="raw_output"),
                verdict_id=_exact_text(raw.get("verdict_id"), label="verdict_id"),
                provider_name=_exact_text(raw.get("provider_name"), label="provider_name"),
                model_name=_exact_text(raw.get("model_name"), label="model_name"),
                policy=policy,
            )
        )
        bindings.append(
            EvaluationCaseBinding(
                case_id=case_id,
                artifact_version_id=artifact_id,
                artifact_sha256=artifact_hash,
                segment_id=segment.segment_id,
                segment_text_hash=segment.text_hash,
                expected_locator=segment.locator,
            )
        )
    manifest = TriageEvaluationManifest(
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_kind="SYNTHETIC_FIXTURE",
        scorer_version=SCORER_VERSION,
        artifacts=(
            EvaluationArtifactDescriptor(
                artifact_version_id=artifact_id,
                original_filename=filename,
                mime_type=mime_type,
                byte_size=len(content),
                content_sha256=artifact_hash,
            ),
        ),
        cases=tuple(bindings),
    )
    return FrozenTriageEvaluationSet(
        manifest=manifest,
        gold=gold,
        thresholds=thresholds,
        predictions=tuple(predictions),
        artifacts=(
            EvaluationArtifactBytes(
                artifact_version_id=artifact_id,
                content_bytes=content,
            ),
        ),
    )


def load_trusted_evaluation_registry(path: str | Path) -> TrustedEvaluationRegistry:
    """Load an outer-layer trusted registry with duplicate-key rejection."""

    payload = _strict_json(path)
    if payload.get("registry_version") != TRUSTED_REGISTRY_VERSION:
        raise ValueError("trusted registry version is unsupported")
    raw_seals = payload.get("representative_seals")
    if type(raw_seals) is not list:
        raise ValueError("trusted registry representative_seals must be an array")
    seals: list[TrustedRepresentativeSeal] = []
    for raw in raw_seals:
        if type(raw) is not dict:
            raise ValueError("each trusted representative seal must be an object")
        normalized = dict(raw)
        for field_name in (
            "manifest_frozen_at",
            "gold_frozen_at",
            "thresholds_frozen_at",
            "predictions_frozen_at",
        ):
            normalized[field_name] = _parse_utc_timestamp(
                normalized.get(field_name), label=field_name
            )
        seals.append(TrustedRepresentativeSeal.model_validate(normalized))
    return TrustedEvaluationRegistry(
        registry_version=TRUSTED_REGISTRY_VERSION,
        representative_seals=tuple(seals),
    )


def _parse_utc_timestamp(value: object, *, label: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError(f"{label} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid RFC 3339 UTC timestamp") from exc
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{label} must be UTC")
    return parsed


__all__ = [
    "CONTENT_ROLES",
    "MINIMUM_PREREGISTERED_COVERAGE",
    "SCORER_VERSION",
    "TRUSTED_REGISTRY_VERSION",
    "CalibrationBin",
    "EvaluationArtifactBytes",
    "EvaluationArtifactDescriptor",
    "EvaluationCaseBinding",
    "EvaluationClaimTier",
    "EvaluationSealError",
    "FrozenTriageEvaluationSet",
    "ProductionTriagePrediction",
    "QualityEvidenceStatus",
    "QualityGateCheck",
    "RiskCoveragePoint",
    "TriageEvaluationManifest",
    "TriageEvaluationMetrics",
    "TriageEvaluationReport",
    "TriageGoldAnnotation",
    "TriageGoldSet",
    "TriageQualityThresholds",
    "TrustedEvaluationRegistry",
    "TrustedRepresentativeSeal",
    "adapt_production_output",
    "build_trusted_representative_seal",
    "compute_artifact_set_sha256",
    "compute_gold_sha256",
    "compute_manifest_sha256",
    "compute_ordered_case_ids_sha256",
    "compute_predictions_sha256",
    "compute_provider_binding_sha256",
    "compute_thresholds_sha256",
    "evaluate_frozen_triage",
    "load_frozen_triage_evaluation_set",
    "load_trusted_evaluation_registry",
    "verify_trusted_representative_seal",
]
