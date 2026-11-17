"""Application service for upload, triage, rights, and human intake decisions."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from ..contracts import OwnershipScope
from .contracts import PDF_PROCESSING_FAILURE_CODES, SourceSegment, TriageVerdict
from .parsers import (
    IntakePolicy,
    IntakeValidationError,
    UploadAccumulator,
    ValidatedUpload,
    parse_source_segments,
    validate_upload_chunks,
)
from .triage import (
    TriagePolicy,
    TriageProvider,
    TriageRequest,
    build_triage_request,
    derive_triage_verdict,
)

RightsProvenance = Literal["OWNED", "LICENSED", "THIRD_PARTY", "UNKNOWN"]
UseKind = Literal["ANALYSIS", "RETRIEVAL", "DRAFTING", "TRAINING", "PUBLICATION"]
DecisionKind = Literal["CONFIRM", "OVERRIDE", "REJECT"]
ContentRole = Literal["CANON_CANDIDATE", "REFERENCE", "MIXED", "UNKNOWN"]
LineageKind = Literal["SOURCE_EVIDENCE", "INSPIRATION_LINEAGE"]

PROVIDER_FAILURE_RAW_OUTPUT = '{"error_code":"PROVIDER_FAILURE"}'

ALLOWED_USE_KINDS: tuple[UseKind, ...] = (
    "ANALYSIS",
    "RETRIEVAL",
    "DRAFTING",
    "TRAINING",
    "PUBLICATION",
)
_RIGHTS_PROVENANCE = frozenset({"OWNED", "LICENSED", "THIRD_PARTY", "UNKNOWN"})
_DECISIONS = frozenset({"CONFIRM", "OVERRIDE", "REJECT"})
_CONTENT_ROLES = frozenset({"CANON_CANDIDATE", "REFERENCE", "MIXED", "UNKNOWN"})


def _canonical_uuid(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UUID string") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError(f"{label} must be a canonical non-nil UUID string")
    return value


def _exact_text(value: object, *, label: str, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        qualifier = "an exact string" if allow_empty else "a non-empty exact string"
        raise ValueError(f"{label} must be {qualifier}")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} contains a lone Unicode surrogate") from exc
    return value


@dataclass(frozen=True, slots=True)
class SegmentDecision:
    segment_id: str
    decision: DecisionKind
    content_role: ContentRole
    rationale: str

    def __post_init__(self) -> None:
        _canonical_uuid(self.segment_id, label="segment_id")
        if self.decision not in _DECISIONS:
            raise ValueError("decision is not supported")
        if self.content_role not in _CONTENT_ROLES:
            raise ValueError("content_role is not supported")
        _exact_text(self.rationale, label="rationale")


class IntakeRepository(Protocol):
    def create_uploaded_run(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        artifact_id: str,
        artifact_version_id: str,
        run_id: str,
        upload: ValidatedUpload,
    ) -> None: ...

    def create_replacement_uploaded_run(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        artifact_id: str,
        artifact_version_id: str,
        run_id: str,
        upload: ValidatedUpload,
    ) -> None: ...

    def store_segments(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        artifact_version_id: str,
        segments: tuple[SourceSegment, ...],
    ) -> None: ...

    def mark_ocr_required(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
    ) -> None: ...

    def mark_processing_failed(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        processing_code: str,
    ) -> None: ...

    def store_raw_triage_output(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        artifact_version_id: str,
        segment_id: str,
        raw_output_id: str,
        provider: str,
        model: str,
        output_text: str,
        output_hash: str,
    ) -> None: ...

    def store_triage_verdict(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        artifact_version_id: str,
        verdict: TriageVerdict,
    ) -> None: ...

    def mark_awaiting_human(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
    ) -> None: ...

    def read_intake_run(
        self,
        scope: OwnershipScope,
        *,
        subject_id: str,
        run_id: str,
    ) -> Mapping[str, object]: ...

    def declare_rights(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        rights_declaration_id: str,
        rights_provenance: RightsProvenance,
        declaration_text: str,
        allowed_uses: Mapping[UseKind, bool],
    ) -> Mapping[str, object]: ...

    def submit_human_decisions(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        decisions: tuple[SegmentDecision, ...],
    ) -> Mapping[str, object]: ...

    def logical_delete_source_artifact(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
    ) -> Mapping[str, object]: ...

    def create_lineage_anchor(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        segment_id: str,
        lineage_kind: LineageKind,
        target_ref: str,
    ) -> Mapping[str, object]: ...

    def is_use_allowed(
        self,
        scope: OwnershipScope,
        *,
        subject_id: str,
        run_id: str,
        use_kind: UseKind,
    ) -> bool: ...


class AbstainingTriageProvider:
    """Zero-cost default: persist an empty raw result and derive UNKNOWN+abstain."""

    name = "not-configured"
    model = "none"

    def classify(self, request: TriageRequest) -> str:
        return ""


class TeamIntakeService:
    def __init__(
        self,
        repository: IntakeRepository,
        *,
        intake_policy: IntakePolicy | None = None,
        triage_policy: TriagePolicy | None = None,
        triage_provider: TriageProvider | None = None,
    ) -> None:
        self.repository = repository
        self.intake_policy = intake_policy or IntakePolicy.from_env()
        self.triage_policy = triage_policy or TriagePolicy()
        self.triage_provider = triage_provider or AbstainingTriageProvider()

    def create_intake_run(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        original_filename: str,
        mime_type: str,
        chunks: Iterable[bytes],
    ) -> Mapping[str, object]:
        upload = validate_upload_chunks(
            chunks,
            original_filename=original_filename,
            mime_type=mime_type,
            policy=self.intake_policy,
        )
        return self._persist_validated_upload(scope, actor_subject=actor_subject, upload=upload)

    async def create_intake_run_async(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        original_filename: str,
        mime_type: str,
        chunks: AsyncIterable[bytes],
    ) -> Mapping[str, object]:
        accumulator = UploadAccumulator(self.intake_policy)
        async for chunk in chunks:
            accumulator.feed(chunk)
        upload = accumulator.finish(
            original_filename=original_filename,
            mime_type=mime_type,
        )
        return self._persist_validated_upload(scope, actor_subject=actor_subject, upload=upload)

    def create_replacement_intake_run(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        artifact_id: str,
        original_filename: str,
        mime_type: str,
        chunks: Iterable[bytes],
    ) -> Mapping[str, object]:
        upload = validate_upload_chunks(
            chunks,
            original_filename=original_filename,
            mime_type=mime_type,
            policy=self.intake_policy,
        )
        return self._persist_validated_upload(
            scope,
            actor_subject=actor_subject,
            upload=upload,
            existing_artifact_id=_canonical_uuid(artifact_id, label="artifact_id"),
        )

    async def create_replacement_intake_run_async(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        artifact_id: str,
        original_filename: str,
        mime_type: str,
        chunks: AsyncIterable[bytes],
    ) -> Mapping[str, object]:
        accumulator = UploadAccumulator(self.intake_policy)
        async for chunk in chunks:
            accumulator.feed(chunk)
        upload = accumulator.finish(
            original_filename=original_filename,
            mime_type=mime_type,
        )
        return self._persist_validated_upload(
            scope,
            actor_subject=actor_subject,
            upload=upload,
            existing_artifact_id=_canonical_uuid(artifact_id, label="artifact_id"),
        )

    def read_intake_run(
        self,
        scope: OwnershipScope,
        *,
        subject_id: str,
        run_id: str,
    ) -> Mapping[str, object]:
        checked_scope = OwnershipScope.model_validate(scope)
        subject = _canonical_uuid(subject_id, label="subject_id")
        checked_run = _canonical_uuid(run_id, label="run_id")
        return self.repository.read_intake_run(
            checked_scope,
            subject_id=subject,
            run_id=checked_run,
        )

    def declare_rights(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        rights_provenance: RightsProvenance,
        declaration_text: str,
        allowed_uses: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        checked_scope = OwnershipScope.model_validate(scope)
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        checked_run = _canonical_uuid(run_id, label="run_id")
        provenance = _exact_text(rights_provenance, label="rights_provenance")
        if provenance not in _RIGHTS_PROVENANCE:
            raise ValueError("rights_provenance is not supported")
        declaration = _exact_text(declaration_text, label="declaration_text")
        normalized = _normalize_allowed_uses(allowed_uses)
        return self.repository.declare_rights(
            checked_scope,
            actor_subject=actor,
            run_id=checked_run,
            rights_declaration_id=str(uuid4()),
            rights_provenance=cast(RightsProvenance, provenance),
            declaration_text=declaration,
            allowed_uses=normalized,
        )

    def submit_human_decisions(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        decisions: Iterable[SegmentDecision],
    ) -> Mapping[str, object]:
        checked_scope = OwnershipScope.model_validate(scope)
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        checked_run = _canonical_uuid(run_id, label="run_id")
        checked_decisions = tuple(
            SegmentDecision(
                segment_id=decision.segment_id,
                decision=decision.decision,
                content_role=decision.content_role,
                rationale=decision.rationale,
            )
            for decision in decisions
        )
        if not checked_decisions:
            raise ValueError("at least one human segment decision is required")
        if len({item.segment_id for item in checked_decisions}) != len(checked_decisions):
            raise ValueError("human decisions contain a duplicate segment_id")
        return self.repository.submit_human_decisions(
            checked_scope,
            actor_subject=actor,
            run_id=checked_run,
            decisions=checked_decisions,
        )

    def logical_delete_source_artifact(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
    ) -> Mapping[str, object]:
        return self.repository.logical_delete_source_artifact(
            OwnershipScope.model_validate(scope),
            actor_subject=_canonical_uuid(actor_subject, label="actor_subject"),
            run_id=_canonical_uuid(run_id, label="run_id"),
        )

    def create_lineage_anchor(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        segment_id: str,
        lineage_kind: LineageKind,
        target_ref: str,
    ) -> Mapping[str, object]:
        if lineage_kind not in {"SOURCE_EVIDENCE", "INSPIRATION_LINEAGE"}:
            raise ValueError("lineage_kind is not supported")
        return self.repository.create_lineage_anchor(
            OwnershipScope.model_validate(scope),
            actor_subject=_canonical_uuid(actor_subject, label="actor_subject"),
            run_id=_canonical_uuid(run_id, label="run_id"),
            segment_id=_canonical_uuid(segment_id, label="segment_id"),
            lineage_kind=lineage_kind,
            target_ref=_exact_text(target_ref, label="target_ref"),
        )

    def is_use_allowed(
        self,
        scope: OwnershipScope,
        *,
        subject_id: str,
        run_id: str,
        use_kind: UseKind,
    ) -> bool:
        if use_kind not in ALLOWED_USE_KINDS:
            raise ValueError("use_kind is not supported")
        return self.repository.is_use_allowed(
            OwnershipScope.model_validate(scope),
            subject_id=_canonical_uuid(subject_id, label="subject_id"),
            run_id=_canonical_uuid(run_id, label="run_id"),
            use_kind=use_kind,
        )

    def _persist_validated_upload(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        upload: ValidatedUpload,
        existing_artifact_id: str | None = None,
    ) -> Mapping[str, object]:
        checked_scope = OwnershipScope.model_validate(scope)
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        artifact_id = existing_artifact_id or str(uuid4())
        artifact_version_id = str(uuid4())
        run_id = str(uuid4())
        durable_pdf_handoff = upload.extension == ".pdf"
        segments: tuple[SourceSegment, ...] | None = None
        if not durable_pdf_handoff:
            segments = parse_source_segments(
                upload,
                artifact_version_id=artifact_version_id,
                policy=self.intake_policy,
            )

        create_method = (
            self.repository.create_replacement_uploaded_run
            if existing_artifact_id is not None
            else self.repository.create_uploaded_run
        )
        create_method(
            checked_scope,
            actor_subject=actor,
            artifact_id=artifact_id,
            artifact_version_id=artifact_version_id,
            run_id=run_id,
            upload=upload,
        )
        if durable_pdf_handoff:
            try:
                segments = parse_source_segments(
                    upload,
                    artifact_version_id=artifact_version_id,
                    policy=self.intake_policy,
                )
            except IntakeValidationError as exc:
                if exc.code == "PDF_OCR_REQUIRED":
                    self.repository.mark_ocr_required(
                        checked_scope,
                        actor_subject=actor,
                        run_id=run_id,
                    )
                else:
                    processing_code = (
                        exc.code
                        if exc.code in PDF_PROCESSING_FAILURE_CODES
                        else "PDF_PROCESSING_FAILED"
                    )
                    self.repository.mark_processing_failed(
                        checked_scope,
                        actor_subject=actor,
                        run_id=run_id,
                        processing_code=processing_code,
                    )
                return self.repository.read_intake_run(
                    checked_scope,
                    subject_id=actor,
                    run_id=run_id,
                )
            except Exception:
                self.repository.mark_processing_failed(
                    checked_scope,
                    actor_subject=actor,
                    run_id=run_id,
                    processing_code="PDF_PROCESSING_FAILED",
                )
                raise
        if segments is None:  # defensive guard for future upload formats
            raise RuntimeError("validated upload did not produce a segment decision")
        self.repository.store_segments(
            checked_scope,
            actor_subject=actor,
            run_id=run_id,
            artifact_version_id=artifact_version_id,
            segments=segments,
        )
        provider = _exact_text(self.triage_provider.name, label="triage provider")
        model = _exact_text(self.triage_provider.model, label="triage model")
        for segment in segments:
            raw_output = self._invoke_triage(segment)
            raw_output_id = str(uuid4())
            output_hash = hashlib.sha256(raw_output.encode("utf-8")).hexdigest()
            # Raw text is committed before any parsing or derived-verdict write.
            self.repository.store_raw_triage_output(
                checked_scope,
                actor_subject=actor,
                run_id=run_id,
                artifact_version_id=artifact_version_id,
                segment_id=segment.segment_id,
                raw_output_id=raw_output_id,
                provider=provider,
                model=model,
                output_text=raw_output,
                output_hash=output_hash,
            )
            verdict = derive_triage_verdict(
                raw_output_id=raw_output_id,
                raw_output=raw_output,
                segment=segment,
                policy=self.triage_policy,
            )
            self.repository.store_triage_verdict(
                checked_scope,
                actor_subject=actor,
                run_id=run_id,
                artifact_version_id=artifact_version_id,
                verdict=verdict,
            )
        self.repository.mark_awaiting_human(
            checked_scope,
            actor_subject=actor,
            run_id=run_id,
        )
        return self.repository.read_intake_run(
            checked_scope,
            subject_id=actor,
            run_id=run_id,
        )

    def _invoke_triage(self, segment: SourceSegment) -> str:
        try:
            output = self.triage_provider.classify(build_triage_request(segment))
            return _exact_text(output, label="raw triage output", allow_empty=True)
        except Exception:  # noqa: BLE001 - persist a distinct fail-closed provider marker
            return PROVIDER_FAILURE_RAW_OUTPUT


def _normalize_allowed_uses(
    supplied: Mapping[str, object] | None,
) -> dict[UseKind, bool]:
    normalized: dict[UseKind, bool] = {kind: False for kind in ALLOWED_USE_KINDS}
    if supplied is None:
        return normalized
    for key, value in supplied.items():
        if key not in ALLOWED_USE_KINDS:
            raise ValueError("allowed_uses contains an unsupported use kind")
        if type(value) is not bool:
            raise ValueError("allowed_uses values must be exact booleans")
        normalized[key] = value
    return normalized


__all__ = [
    "ALLOWED_USE_KINDS",
    "AbstainingTriageProvider",
    "ContentRole",
    "DecisionKind",
    "IntakeRepository",
    "LineageKind",
    "PROVIDER_FAILURE_RAW_OUTPUT",
    "RightsProvenance",
    "SegmentDecision",
    "TeamIntakeService",
    "UseKind",
]
